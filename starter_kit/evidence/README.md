# LoomQ 人工评分证据

Team ID：`wildernotrack` · Fork：https://github.com/WilderNoTrack/LoomQ-2026

## 申报项目

- [x] L1 真机（量旋云已完成；本源悟空芯片维护中，恢复后补入）
- [x] L2 交互体验
- [x] 工程与产品化
- [x] 自定义量子 RISC-V Bonus
- [x] 新手引导与视觉叙事 Bonus

---

## L1 真机

### 量旋云 · 3 比特核磁真机（GHZ-3）

```text
平台名称：spinq_cloud_qpu（量旋云 triangulum_vp，3Qubit 核磁量子计算机）
平台 job ID：S-260813-0001
运行时间：2026-08-13T03:30:17Z（UTC+8 为 2026-08-13 11:30:17）
shots：1024
实际执行的 QASM：starter_kit/evidence/files/spinq-ghz3-circuit.qasm
平台返回的原始结果：starter_kit/evidence/files/spinq-ghz3-result.json
```

主峰核验：理想分布为 `000` / `111` 各 50%；真机返回 `000` 474 次（46.29%）、
`111` 480 次（46.88%），**Top-2 主峰完全命中**，其余 6 个态合计 6.8% 为硬件噪声。

### 量旋云 · 2 比特核磁真机（Bell）

```text
平台名称：spinq_cloud_qpu（量旋云 gemini_vp，2Qubit 核磁量子计算机）
平台 job ID：G-260813-0009
运行时间：2026-08-13T03:36:31Z（UTC+8 为 2026-08-13 11:36:31）
shots：1024
实际执行的 QASM：starter_kit/evidence/files/spinq-bell-circuit.qasm
平台返回的原始结果：starter_kit/evidence/files/spinq-bell-result.json
```

主峰核验：理想分布为 `00` / `11` 各 50%；真机返回 `11` 511 次（49.9%）、
`00` 439 次（42.9%），**Top-2 主峰完全命中**，`01`/`10` 合计 7.2% 为硬件噪声。

两次任务均可在量旋云控制台以上述 job ID 溯源，账号 `WilderNoTrack`。

### 本源量子云 · 悟空 72 比特超导真机

提交链路已打通并验证：OriginIR 文本直接进入 `real_chip_measure` 的 `code` 字段——
**评分产物与真机执行的字符串是同一个**。当前平台返回：

```text
{"code":20045,"message":"Quantum computer under maintenance. Please try again later."}
```

芯片维护结束后重跑同一条命令即可，结果会自动写入本目录：

```bash
python3 tools/hardware_run.py --target originq --file circuits/bell.qasm --shots 1000
```

### 复现方式

同一份中间层，只需切换执行器；凭证只来自本机 `secrets.env`（已被 .gitignore 排除）：

```bash
python3 tools/hardware_run.py --status                       # 看凭证与平台就绪状态
python3 tools/hardware_run.py --target spinq  --file circuits/ghz3.qasm --shots 1024
python3 tools/hardware_run.py --target originq --file circuits/bell.qasm --shots 1000
```

三个真机接入过程中修掉的平台特性（都写在 `loomq/backends/hardware.py` 注释里）：

- 量旋云**拒绝显式 `measure` 门**（"A measure will be done automatically"），
  故提交时剥离测量语句，再按电路自己的 qubit→clbit 映射把结果重新编码；
- 量旋云的 `machine_count` 会为 0（8 比特超导机当前无可用机器），
  且各真机门集远窄于 12 门白名单（2 比特机无 S/T，8 比特超导机无 CNOT），
  故选型同时按比特数、门集、可用机器数三重过滤；
- 真机任务号在 `result.task_code`（如 `S-260813-0001`），不在 `task_id`；
  取不到任务号时直接报错而不是伪造一个，因为无法溯源的证据按无效处理；
- 本源 QCloud 的 `set_configure(72,72)` 会**反向清除**初始化状态，导致后续调用
  报 "Must initialize the system first"，故不调用它；
  且 `init_qvm(token, True)` 会开启 DEBUG 日志并**明文打印 API Token**，故日志保持关闭。

---

## L2 交互体验

```text
启动界面或 CLI 的命令：
  cd starter_kit && python3 -m loomq web
  （零依赖，标准库 HTTP 服务；也可用 python3 -m loomq chat 走命令行）

测试入口或页面地址：http://127.0.0.1:8787/

用于交互体验评测的 3 个用户任务：
1. 零知识入门：点页面上的「第一次来？看 3 步演示」。
   预期：三个电路图 + 三张分布图，全部当场计算，不需要任何输入、不需要联网。
2. 一句话生成：在输入框输入「做一个 4 比特的 GHZ 纠缠态并全部测量」。
   预期：人话解释 + 可运行 QASM + SVG 电路图 + `0000`/`1111` 各 50% 的直方图；
   点「运行」得到真实 counts；点「看三家平台的原生指令」看到三种方言并排。
3. 出错求助：输入「我想制备一个贝尔态，但这段代码报错了，帮我修好：
   H q[0]; CX q[0] q[1]」。
   预期：指出缺寄存器声明与门名大小写，给出修好的、已被模拟核对过的电路。

截图或演示视频：无（界面为可运行代码，请直接启动评测）
```

未配置 `LOOMQ_LLM_*` 时，任务 1 与全部电路功能仍然可用，页面会明确说明缺什么。
设计理由与无障碍细节见 [`../INCLUSION.md`](../INCLUSION.md)。

---

## 工程与产品化

```text
干净环境中的构建和启动命令：
  cd starter_kit
  python3 -m loomq selftest                 # 全量自检，无需安装任何依赖
  python3 evaluator.py --json-out report.json
  docker build -t loomq-submission . && docker run --rm loomq-submission

架构说明：../ARCHITECTURE.md
  一条管线（解析 → 降级 → 发射/执行 → 归一化），平台差异只存在于
  loomq/emitters/<vendor>.py 与 loomq/backends/<vendor>_backend.py 两个文件。
  门重写、位序归一化、结果 Schema 全仓库各只有一份实现。

目标用户和使用场景：../INCLUSION.md
  有具体问题但没有科班通道的人——研究生、中学教师、跨界创作者、转行工程师。

完整使用流程：../README.md
  python3 -m loomq tour → web → ask → run → transpile → doctor
```

自测状态（CPython 3.14 与 3.10.11 均实跑通过）：

- `python3 -m unittest discover -s tests -t .` — **107 个用例全部通过**
- 官方 `evaluator.py --level l1 --target spinq,originq,braket` — **6/6 PASS**；`--level l3` — **1/1 PASS**
- 8 类电路 × 3 个目标 IR 回环等价 — **24/24 精确相等（保真度 1.0）**
- 随机 Hybrid-QASM 程序 × 全部测量注入组合 — **全部与参考解释器一致**

**厂商 SDK 实测**（`tools/validate_vendor_ir.py`，把 `transpile()` 的产物交给厂商自己的解析器）：

```text
circuit      vendor    fidelity
bell/ghz3/ghz5/qft4/grover3/random×3/whitelist
             spinq     ≥ 0.99982   （spinqit 0.2.4 的 QASM 编译器 + BasicSimulator）
             originq   ≥ 0.98405   （pyqpanda 3.8.5 的 OriginIR 导入器 + CPUQVM）
             braket    ≥ 0.98661   （amazon-braket-sdk 1.110.1 的 OpenQASM 3 解析器）
27/27 通过
```

真实 SDK 执行路径（`LOOMQ_EXECUTOR=sdk`）：

```text
originq + braket  evaluator.py --level l1 → 4/4 PASS   （meta.executor 为真实 SDK）
spinq             evaluator.py --level l1 → 2/2 PASS   （meta.bit_order_reversed = true）
```

**L2 真实模型实测**（`tools/l2_live_check.py`，DeepSeek `deepseek-v4-flash`）：

16 个 prompt 变体，按组委会的判定方式打分——回复经官方 `extract_qasm()` 正则提取、
解析、模拟，与请求所要求的分布比对；选型题必须命中正确规范标识且不得给出错误标识。

```text
8 个意图生成（GHZ-3/4/5、Bell、W-3、|101>、|1101>、4 比特均匀叠加）  全部 fidelity 1.0000
4 个代码纠错（缺头/缺测量/写错比特/大小写）                          全部 fidelity 1.0000
4 个后端选型（含「26 比特不排队」唯一解陷阱题）                       全部命中且无误报
16/16 通过（100%），单 case 平均 6.6 秒（限额 120 秒）
```

注：`spinqit` 与 `amazon-braket-sdk` 因 `antlr4-python3-runtime` 版本精确锁定冲突
（4.9.2 vs 4.13.2）无法共存于同一环境，故分为两个 requirements 文件、两个 venv。
详见 [`../ARCHITECTURE.md`](../ARCHITECTURE.md) 第 9 节。

---

## 自定义量子 RISC-V Bonus

```text
指令编码规格：../QISA.md
  custom-0 操作码空间 (0b0001011)，标准 R-type 字段布局，
  funct3 选指令类、funct7 选具体操作，覆盖 12 门白名单 + 初始化/复位/测量/采样。
  角度用定点数：寄存器值 k 表示 k·π/4096 弧度。

模拟器扩展实现：../riscv_emulator_loomq.py
  继承官方 TinyRISCVEmulator（不是复制），经典指令语义逐字节不变，
  只增加量子寄存器堆与 custom-0 解码器。助记符与 .word 原始编码等价执行。

端到端测试命令：
  cd starter_kit
  python3 riscv_emulator_loomq.py                                    # 冒烟测试
  python3 -m loomq qisa examples/hybrid_bell.hqasm --listing         # 编码对照
  python3 -m loomq qisa examples/hybrid_whitelist.hqasm --run --shots 2000
  python3 -m unittest tests.test_qisa -v                             # 19 个用例
```

核心价值：L3 契约把混合程序拆成两半、由评测系统从外部注入测量值；
LoomQ-Q 把这条缝补上——`qmeas` 直接写 `x10`，读 `x10` 的经典块就是下一条指令。
**一个程序，一条指令流，一个程序计数器**，分支之所以走这一边，
是因为那个量子比特真的塌缩成了 1。

`--run` 会把数千次执行的测量分布与内置精确模拟器对照并给出 Hellinger 保真度。

---

## 新手引导与视觉叙事 Bonus

```text
零基础首次运行指南：
  python3 -m loomq tour        终端版，四步，每步真跑一次电路，不联网不注册
  python3 -m loomq web         网页版「第一次来？看 3 步演示」按钮

量子概念解释：
  ../INCLUSION.md（为谁而做、五道门槛怎么拆）
  loomq/tour.py 的四步文案：把叠加解释成「量子硬币」，
  把纠缠解释成「不是两个都随机，而是两个一起随机」，
  并当场指出 `01` 和 `10` 一次都没出现作为证据

结果可视化：
  loomq/diagram.py — 同一份排布代码产出两种渲染：
    终端 ASCII 电路图（python3 -m loomq show circuits/bell.qasm）
    网页 SVG 电路图（跟随明暗主题，带 role="img" 与 aria-label）
  分布直方图在 CLI 与网页两端都有

错误恢复或无障碍引导：
  loomq/errors.py + loomq/qasm/parser.py — 报错带行列号、原始行和修复建议，
    并作为产品功能被测试（tests/test_qasm_frontend.py::DiagnosticTests）
  同一份诊断信息驱动 L2 的纠错回炉，模型拿到的是具体错误而不是"失败了"
  网页端：键盘全可达、焦点描边、Ctrl+Enter 提交、prefers-reduced-motion、
    明暗双主题、零外部资源（断网可用，不上传任何内容）
```

---

## 提交合规

- 仓库内不含任何 API Key、Token、Cookie 或个人身份信息；
  L2 配置全部来自 `LOOMQ_LLM_*` 环境变量，错误信息中不会回显凭证
  （`tests/test_l2_agent.py::test_configuration_errors_never_contain_the_credential`）。
- 归档大小远低于 100 MiB（纯文本源码，无二进制附件）。
- 按第六节第 4 条：本项目使用了 AI 辅助编程，
  系统各部分的工作原理与验证方法记录在 [`../ARCHITECTURE.md`](../ARCHITECTURE.md)。
