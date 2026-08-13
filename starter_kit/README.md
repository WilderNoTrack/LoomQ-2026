# LoomQ · 量子接入平权计划

> 一个中间层，三家平台。把「想做什么」翻译成量子计算机听得懂的话。

每个量子云平台都在筑自己的黑话高墙：本源用 `OriginIR`，量旋用 SpinQit，AWS Braket
用 OpenQASM 3。LoomQ 把这堵墙拆成一层薄薄的翻译层——你写一次标准 OpenQASM 2.0，
或者干脆用一句中文描述，剩下的交给它。

**LoomQ 让哪一类原本进不来的人第一次能用上量子计算？**
答案写在 [`INCLUSION.md`](INCLUSION.md)，那是这个项目真正的题眼。

---

## 一条命令跑起来

不需要装任何第三方包，不需要注册任何账号，不需要联网。

```bash
cd starter_kit
python3 -m loomq tour          # 零基础导览：四步，每步都真跑一次电路
python3 -m loomq web           # 浏览器界面，用一句话生成并运行电路
python3 -m loomq selftest      # 全量自检：8 类电路 × 3 个后端 + 随机 L3 用例
```

官方公开自测：

```bash
python3 evaluator.py --level l1 --target spinq,originq,braket --json-out report.json
python3 evaluator.py --level l3
```

容器（与正式评测同一基础镜像）：

```bash
docker build -t loomq-submission .
docker run --rm loomq-submission
```

---

## 三个 Level 的入口

提交契约的四个函数都在 [`adapter.py`](adapter.py)，本体在 `loomq/`：

| 契约函数 | 实现位置 | 说明 |
|---|---|---|
| `transpile(qasm, target)` | `loomq/emitters/` | OpenQASM 2.0 → SpinQ QASM2 / OriginIR / Braket QASM3 |
| `run(qasm, target, shots)` | `loomq/execution.py` | 执行并归一化成统一 JSON Schema |
| `agent_chat(prompt)` | `loomq/agent/` | 自然语言 → 可运行电路 / 纠错 / 选后端 |
| `compile_hybrid(source)` | `loomq/hybrid/` | Hybrid-QASM → 量子操作序列 + RISC-V 汇编 |

---

## 架构：一条管线，三个出口

```
OpenQASM 2.0
     │
     ├─ loomq/qasm/        词法 + 递归下降语法分析（含自定义门内联、参数表达式）
     │                     错误带行列号和修复建议，这是 L2 纠错能力的基础
     ↓
  Circuit IR              loomq/ir.py — 平台无关，扁平比特索引 + 保留用户寄存器名
     │
     ├─ loomq/passes/      降级到 12 门白名单：具名恒等式 → ZYZ 欧拉分解 → ABC 受控构造
     │                     + peephole 清理（去掉恒等旋转、合并同轴旋转）
     ↓
  Lowered Circuit
     │
     ├─ loomq/emitters/    三个「打印机」：spinq.py / originq.py / braket.py
     │                     它们只决定怎么拼字符串，不决定用哪些门
     │
     └─ loomq/backends/    三个厂商适配器 + 内置参考模拟器
                           └─ loomq/execution.py 统一归一化位序、组装 Schema
```

**为什么这叫"通用"而不是三套硬编码**：整个仓库里只有一处门重写逻辑
（`loomq/passes/decompose.py`）、一处结果归一化逻辑（`loomq/execution.py`）。
新增第四个平台 = 写一个 emitter（约 60 行）+ 一个 backend（约 60 行），
不需要动前端、不需要动降级、不需要动 Schema。

详细设计与取舍见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

---

## 关键设计决定

**执行器与自校验。** `run()` 默认策略是 `auto`：厂商 SDK 装得上就用厂商 SDK，
装不上就用 LoomQ 内置的精确态矢模拟器。无论走哪条路，结果都会和内置模拟器
算出的精确分布对照一次——位序反了会被自动纠正（这正是题面要求中间层承担的归一化），
门翻译错了会被拦下来并回退。判定阈值按散粒噪声推导，不是拍脑袋的常数，
见 `loomq/execution.py:acceptance_threshold`。发生了什么全部写进 `meta`。

用 `LOOMQ_EXECUTOR=sdk|reference|auto` 切换；`python3 -m loomq doctor` 看当前状态。

**采样。** 无噪声模拟器知道精确分布，所以默认用分层配额（最大余数法）而不是
伪随机采样——给一个已经精确的数字人为加上噪声没有意义。
`LOOMQ_SAMPLING=multinomial` 可切回真实散粒噪声，真机与厂商 SDK 天然处于该模式。

**智能体：模型负责理解，LoomQ 负责决策。** 一次模型调用把自然语言变成结构化规格，
之后全是确定性代码：认识的态族（GHZ / Bell / W / 均匀叠加 / 基态 / QFT / Grover）
由 LoomQ 按教科书构造直接生成；选后端由官方《后端能力表》按约束筛选。
换一种问法只会改变第一步的输入，不会动摇后两步的正确性。
所有电路在返回给用户之前都被解析、模拟、核对过。

**位序。** counts 的 key 是 `c[n-1]…c[1]c[0]`，**最右边是 c[0]**，`bit_order` 恒为
`"little"`。跨平台差异在 `loomq/execution.py` 里归一化，测试见
`tests/test_l1_pipeline.py::test_counts_key_is_little_endian`。

---

## 测试

```bash
python3 -m unittest discover -s tests -t .    # 107 个用例，纯标准库
```

装上厂商 SDK 后，还可以把 `transpile()` 的产物交给**厂商自己的解析器**验证：

```bash
pip install -r requirements-backends.txt      # pyqpanda + braket
python3 tools/validate_vendor_ir.py --targets originq,braket

python3 -m venv .venv-spinq                   # spinqit 必须单独装，原因见下
.venv-spinq/bin/pip install -r requirements-spinq.txt
.venv-spinq/bin/python tools/validate_vendor_ir.py --targets spinq
```

> **`spinqit` 与 `amazon-braket-sdk` 无法共存**：前者锁 `antlr4-python3-runtime==4.9.2`，
> 后者的模拟器锁 `==4.13.2`，pip 直接 `ResolutionImpossible`。这正是 LoomQ 要解决的问题的
> 具体形状——厂商 SDK 之间连一个解释器都共享不了，而中间层零依赖，
> 所以同一份代码能同时对三家说话。

覆盖的关键不变量：

- **回环等价**：8 类官方电路 × 3 个目标 IR，发射后再解析回来，
  和源电路比较**精确分布**——这正是组委会解析 `transpile()` 产物时做的事；
- **分解正确性**：`GATES` 里每一个门降级到白名单后，与原矩阵只差一个全局相位；
- **L3 穷举**：随机生成的 Hybrid-QASM 程序，穷举所有测量注入组合，
  和参考解释器逐寄存器比对；
- **L2 离线**：本地 OpenAI-compatible 桩服务，断言回复里的程序能被官方
  `extract_qasm()` 正则取出且分布正确，以及密钥永不出现在任何错误信息里。

---

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `LOOMQ_EXECUTOR` | `auto` / `sdk` / `reference` | `auto` |
| `LOOMQ_SAMPLING` | `stratified` / `multinomial` | `stratified` |
| `LOOMQ_SEED` | 多项式采样的随机种子 | 无 |
| `LOOMQ_LLM_BASE_URL` | L2 模型服务根地址 | 组委会注入 |
| `LOOMQ_LLM_API_KEY` | L2 凭证 | 组委会注入 |
| `LOOMQ_LLM_MODEL` | L2 模型名 | 组委会注入 |
| `LOOMQ_LLM_TIMEOUT_SECONDS` | 单次请求超时 | 120 |

L1 与 L3 完全不需要网络。L2 只需要组委会注入的模型服务，
代码里没有任何硬编码的地址、密钥或模型名。

---

## AI 辅助声明

按第六节第 4 条：本项目在开发过程中使用了 AI 辅助编程。系统各部分的工作原理、
关键取舍和验证方法记录在 [`ARCHITECTURE.md`](ARCHITECTURE.md) 中，
每个模块的 docstring 也说明了「为什么这样做」而不只是「做了什么」，供异步审查。

---

## 相关文档

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — 设计、取舍、验证策略
- [`INCLUSION.md`](INCLUSION.md) — 为谁而做（必答题）
- [`QISA.md`](QISA.md) — 自定义量子 RISC-V 扩展指令规格（Bonus）
- [`evidence/README.md`](evidence/README.md) — 人工评分证据包
- [`SUBMISSION_PROTOCOL.md`](SUBMISSION_PROTOCOL.md) — 官方 Starter Kit 原始说明（提交协议）
- [`QUANTUM_101.md`](QUANTUM_101.md) · [`gate_identities.md`](gate_identities.md) ·
  [`target_ir_contract.md`](target_ir_contract.md) — 官方参考资料
