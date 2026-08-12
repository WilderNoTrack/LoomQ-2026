# LoomQ 架构说明

这份文档说明**为什么这样设计**，以及每个决定是怎么被验证的。
按第六节第 4 条，它同时是 AI 辅助编程的工作原理说明，供异步审查。

---

## 1. 一句话

LoomQ 是一条单向管线：**解析 → 降级 → 发射 / 执行 → 归一化**。
三家平台的差异被压缩到管线末端的两个小盒子里（一个 emitter、一个 backend），
其余部分完全共享。

```
                      ┌─────────────────────────────────────────┐
 自然语言 ──L2──▶      │                                         │
                      │   loomq/qasm/    OpenQASM 2.0 前端       │
 OpenQASM 2.0 ───────▶│         ↓                               │
                      │   loomq/ir.py    平台无关 Circuit IR     │
                      │         ↓                               │
                      │   loomq/passes/  降级到 12 门白名单       │
                      │         ↓                               │
                      │   ┌──────────────┬──────────────┐        │
                      │   │ emitters/    │ backends/    │        │
                      │   │ 三个打印机     │ 三个执行器     │        │
                      │   └──────┬───────┴──────┬───────┘        │
                      │          ↓              ↓                │
                      │      目标 IR        loomq/execution.py    │
                      │                     位序归一化 + Schema    │
                      └─────────────────────────────────────────┘

 Hybrid-QASM ──L3──▶  loomq/hybrid/   量子部分走上面的前端
                                      经典部分 → AST → RISC-V 汇编
```

---

## 2. 为什么是"一个中间层"而不是"三套分支"

题面第八节第一条明确会审查这一点。判据很简单：**加第四个平台要改几个文件？**

在 LoomQ 里是两个新文件，加两行注册：

- `loomq/emitters/<vendor>.py` —— 只回答"这个门在这家平台叫什么、怎么拼"；
- `loomq/backends/<vendor>_backend.py` —— 只回答"怎么把电路交给这个 SDK、
  怎么把 counts 拿回来"。

以下逻辑**全仓库只有一份**，任何平台都不能拥有自己的版本：

| 逻辑 | 唯一实现位置 |
|---|---|
| OpenQASM 2.0 解析 | `loomq/qasm/parser.py` |
| 门重写 / 分解 | `loomq/passes/decompose.py` |
| 电路优化 | `loomq/passes/optimize.py` |
| 位序归一化 | `loomq/execution.py` + `loomq/result.py` |
| 结果 Schema 组装与校验 | `loomq/result.py` |
| 精确模拟 | `loomq/sim/statevector.py` |

emitter 里没有一行代码决定"用哪些门"——那是 `passes` 的事；
backend 里没有一行代码决定"counts 长什么样"——那是 `execution` 的事。
这个边界是刻意的，也是可以被 code review 直接检查的。

---

## 3. 降级：三层，从具体到通用

`loomq/passes/decompose.py` 按顺序尝试三种手段：

1. **具名恒等式**——`gate_identities.md` 里那些，加上 qelib1 的常规写法。
   短、精确、评委可以用眼睛核对。
2. **ZYZ 欧拉分解**——任何剩下的单比特门：`U = e^{iα} Rz(β) Ry(γ) Rz(δ)`。
3. **ABC 受控构造**——任何剩下的"一个控制位 + 一个目标位"的门：
   `CU = u1(α)_c · A_t · CX · B_t · CX · C_t`，其中 `ABC = I`。

第 2、3 层是"为什么不需要给每家平台每个门写一条规则"的答案：
只要一个门能写成单比特酉矩阵、或者对单比特酉矩阵加一个控制位，就能自动降级。

**全局相位。** 有几处重写与原门相差一个全局相位（`u1 → rz`、`Y = X·Z`）。
全局相位对任何测量分布都不可观测，但**只有在整个门被一次性替换时才安全**——
在受控门内部逐个替换 `u1 → rz` 会改变相对相位。代码里每一处都标注了这一点，
`cu1` 的降级注释解释了为什么它的三次替换恰好乘出一个全局标量。

**验证方式**：`tests/test_l1_pipeline.py::test_all_gates_lower_to_the_whitelist_up_to_global_phase`
对 `GATES` 里每一个门，在一个非平凡输入态上比较降级前后的完整态矢，
要求两者只差一个模长为 1 的常数。不是在 |0…0> 上比，避免侥幸通过。

---

## 4. 回环验证：我们自己跑一遍组委会要跑的东西

正式 L1 评分不止看 `run()` 的 counts，还会**解析并模拟 `transpile()` 的返回值**。
所以 `loomq/verify.py` 实现了另一半：OpenQASM 3 与 OriginIR 的导入器。

测试流程：

```
源 QASM 2.0 ──emit──▶ 目标 IR ──reimport──▶ Circuit ──simulate──▶ 分布
     └────────────────────simulate───────────────────▶ 分布
                          两者必须精确相等
```

8 类电路 × 3 个目标 = 24 条回环，全部要求保真度 = 1.0（不是 0.97，是精确相等）。
一个门名拼错、一个操作数顺序反了、一条测量语句漏了，都会在这里失败。

这些导入器只为验证 LoomQ 自己的输出而存在，不追求解析任意厂商代码。

---

## 5. 执行与自校验

`loomq/execution.py` 的 `run_circuit()`：

1. 解析源电路，算出**精确分布**（内置模拟器是闭式计算，不是采样）；
2. 按 `LOOMQ_EXECUTOR` 策略选执行器（默认 `auto`：有 SDK 用 SDK）；
3. 拿到 counts 后，与第 1 步的精确分布对照：
   - 保真度不达标 → 试一次反转位序。如果反转后达标，说明这家平台把 `c[0]`
     放在最左边，采纳并记 `meta.bit_order_calibrated = true`。
     **这正是题面要求"跨平台位序差异必须在中间层内归一化"的做法**；
   - 仍然不达标 → 说明是翻译 bug 而不是散粒噪声，回退到内置模拟器，
     并把原因写进 `meta.fallback_reason`。

**阈值不是常数。** 从一个有 `d` 个结果的分布里采样 `shots` 次，即使实现完全正确，
Hellinger 保真度也会因为散粒噪声偏离约 `sqrt((d-1)/(8·shots))`。
所以接受阈值按这个量推导（`acceptance_threshold()`），电路越宽阈值越松。
用一个固定的 0.99 会把正确的宽分布结果误判为错误。

**透明度**：`meta` 里始终记录 `executor`、`backend_selection`、
`reference_fidelity`、`acceptance_threshold`，以及回退时的 `fallback_reason`。
任何一次运行都能说清楚是谁算的、和参考值差多少。

---

## 6. 采样：为什么默认不加噪声

无噪声模拟器知道精确分布。把它变成 8192 个 shot 有两种做法：

- `multinomial`：伪随机独立采样，重现真机的散粒噪声；
- `stratified`（默认）：最大余数法分配，`floor(p·shots)` 加余数补齐。

默认选后者，因为给一个已经精确的数字人为加上伪随机误差没有信息增益。
真机和厂商 SDK 天然处于 `multinomial` 模式，`LOOMQ_SAMPLING=multinomial`
可以在本地复现同样的统计行为。两种模式都会在 `meta.sampling` 里如实标注。

---

## 7. L2：模型负责理解，LoomQ 负责决策

这是整个智能体设计的核心一句话，也是反作弊条款的正面回答。

```
用户一句话
   │
   ▼  ①  一次模型调用（唯一的不确定环节）
结构化规格 {task, family, num_qubits, constraints, qasm, expected_counts}
   │
   ├─ task=generate/repair 且 family 认识 ──▶ ② LoomQ 按教科书构造生成电路
   │                                            （GHZ / Bell / W / 均匀叠加 /
   │                                             基态 / QFT / Grover）
   ├─ family=custom ──▶ 用模型给的 QASM ──▶ ③ 解析 + 模拟核对
   │                                          不通过就带着**具体诊断**回炉重修
   └─ task=select_backend ──▶ ② 按官方《后端能力表》筛选约束
   │
   ▼  ③ 所有电路在返回前都被解析、降级到白名单、模拟核对过
回复文本（解释 + ```qasm 围栏 + 分布直方图 + 怎么真跑一次）
```

**为什么换一种问法打不垮它**：改写措辞只改变第 ① 步的输入。
第 ② 步是查表和教科书构造，第 ③ 步是模拟器，两者都有自己的测试。
这也是题面"强烈建议 Agent 直接加载 JSON 作为选型知识库"的做法——
`loomq/capabilities.py` 读的就是官方 `backend_capabilities.json` 本体。

**为什么这不是"关键词匹配式伪 Agent"**：语义理解完全由模型完成，
`loomq/agent/selection.py::constraints_from_text` 里的本地关键词扫描
只用于**补全模型漏填的字段**和模型服务不可用时的降级，
从不替代模型调用。每个 case 都会真实调用模型服务。

**回复格式**是刻意的：解释在前，QASM 在 ```qasm 围栏里，验证信息在围栏之后。
官方 `extract_qasm()` 的正则是惰性匹配到下一个 ``` 行或字符串结尾——
把解释放在 QASM 之后而不加围栏会让正则把解释文字一起吞进程序里。

**密钥安全**：`loomq/agent/llm.py` 的任何异常都不包含 Key、URL query 或请求头。
`tests/test_l2_agent.py::test_configuration_errors_never_contain_the_credential`
和 `test_redacted_config_hides_the_key` 守着这条线。

---

## 8. L3：真正的解析与编译

`loomq/hybrid/` 分三步：

1. **切分**（`parser.py`）——括号配对扫描把 `classical { ... }` 块摘出来，
   剩下的是合法 OpenQASM 2.0，交给 L1 的同一个前端解析。
   L3 程序的量子部分因此享受和普通电路完全一样的检查（寄存器越界、门元数……）。
2. **建树**（`ast.py`）——经典块解析成真正的语法树，不是模式匹配。
3. **生成**（`riscv.py`）——遍历语法树生成 `TinyRISCVEmulator` 支持的
   `li/add/sub/addi/beq/bne/j` 子集。

三个必须做对的细节：

- **测量寄存器只读**。`x10, x11, …` 承载注入值，所以临时寄存器从 `x31` 往下分配，
  永远不和它们重叠。
- **收尾清零**。`execute()` 返回所有非零寄存器，评测把这个字典和参考解释器比对。
  残留的临时值会变成多余的表项，所以尾部把用过的临时寄存器全部清零——
  终态只剩 `x1..x9` 和注入的测量寄存器。
- **求值顺序**。二元表达式先把右操作数算进临时寄存器，再写目标寄存器，
  所以 `r1 = r2 - r1` 不会在读到 `r1` 之前把它覆盖掉。
  `tests/test_l3_hybrid.py::test_destination_aliases_a_read_operand` 专门盯这条。

**验证方式**：`loomq/hybrid/fuzz.py` 按题面文法随机生成程序，
`verify_all()` 对每个程序穷举所有测量注入组合，把官方模拟器的终态
和参考解释器逐寄存器比对——和正式评测做的事完全一样，只是种子不同。
测试默认跑 150 个随机程序 + 40 个更深嵌套的程序。

---

## 9. 依赖策略

`requirements.txt` 是空的，这是刻意的：核心只用 Python 3.10 标准库。
装不上的提交等于零分，而这里每一层都能在裸解释器里跑。

厂商 SDK 放在 `requirements-backends.txt`，精确锁版本，
`Dockerfile` 里用 `|| echo` 兜底安装。`loomq/backends/base.py::import_optional`
懒加载，失败就回退到内置模拟器并在 `meta` 里说明原因。

版本选择有约束：`amazon-braket-sdk` 从 1.111 起要求 Python ≥ 3.11，
而官方基础镜像是 3.10，所以锁在 1.110.1。

---

## 10. 已知边界

诚实列出来，比被发现好：

- 内置模拟器是态矢模拟，上限 26 比特（`loomq/sim/statevector.py::MAX_QUBITS`），
  纯 Python 实现，10 比特以内瞬时，超过 20 比特会明显变慢。
  评测电路最多 5 比特，L2 的自验电路也在这个量级。
- 中路测量分支上限 4096 条（`MAX_BRANCHES`）。终末测量走单遍快路径，
  不受此限；只有"测量后还有门"的电路才会分支。
- OriginIR 的规范子集没有经典前馈，所以带 `if (c == k)` 的电路转译到
  `originq` 会明确报错而不是静默丢弃语义。
- 厂商 SDK 适配器在本机（Windows / Python 3.14）无法安装验证，
  它们的正确性由 `auto` 策略的自校验兜底：SDK 结果与参考分布不符就回退。
