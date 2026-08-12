# LoomQ-Q：量子 RISC-V 扩展指令规格 v1.0

> Bonus 项（+8）三件套：
> ① 本文档是**指令编码规格**；
> ② [`riscv_emulator_loomq.py`](riscv_emulator_loomq.py) 是**对官方模拟器的扩展实现**；
> ③ [`tests/test_qisa.py`](tests/test_qisa.py) 与 `python3 -m loomq qisa` 是**可运行的端到端测试**。

---

## 1. 为什么要有这个扩展

L3 的契约把一个混合程序拆成两半：一份量子操作序列，一份经典汇编，
测量值由评测系统从外部注入 `x10`。这是可评测的，但**那条缝是假的**——
真实的混合程序里，分支之所以走这一边，是因为那个量子比特真的塌缩成了 1。

LoomQ-Q 把缝补上：量子操作变成和经典指令同一套 32 位编码的真指令，
`qmeas` 把测量结果直接写进 `x10`，读 `x10` 的经典块就是下一条指令。

**一个程序，一条指令流，一个程序计数器。**

```
    li x28, 2
    qinit x28              ← 分配 2 个量子比特
    li x28, 0
    qh x28                 ← H 作用在 q[0]
    qmeas x10, x28         ← 塌缩，结果落进 x10
    li x27, 1
    bne x10, x27, ELSE     ← 经典分支读的就是刚才那次测量
    li x1, 100
    ...
```

---

## 2. 指令格式

全部指令使用 RISC-V 基础 ISA 为非标准扩展保留的 **`custom-0` 操作码
`0b0001011` (0x0B)**，采用标准 R-type 字段布局：

```
 31        25 24     20 19     15 14     12 11      7 6         0
┌────────────┬─────────┬─────────┬─────────┬─────────┬───────────┐
│   funct7   │   rs2   │   rs1   │  funct3 │    rd   │  0001011  │
│   7 bits   │  5 bits │  5 bits │  3 bits │  5 bits │   7 bits  │
└────────────┴─────────┴─────────┴─────────┴─────────┴───────────┘
```

- `funct3` 选择**指令类**（系统 / 单比特 / 带参单比特 / 双比特 / 三比特 / 测量）；
- `funct7` 在类内选择**具体操作**；
- `rd` / `rs1` / `rs2` 一律是**寄存器号**，不是立即数。

**为什么操作数是寄存器而不是立即数**：比特下标放在寄存器里，
`li x28, 0` / `qh x28` / `addi x28, x28, 1` 就是一个真正的「遍历量子比特」循环，
而不是把 N 条指令展开写死。这也和基础 ISA 的风格一致。

**角度用定点数**：寄存器里的整数 `k` 表示 `k · π / 4096` 弧度，
取值折叠到 `(-π, π]`。整套编码因此保持纯整数，与基础 ISA 一致；
4096 级的分辨率比这三家平台任何一台硬件的标定精度都细。

---

## 3. 指令表

| 汇编 | funct3 | funct7 | 语义 |
|---|---|---|---|
| `qinit rs1` | `0b000` | `0x00` | 分配 `x[rs1]` 个量子比特，全部置 \|0⟩ |
| `qreset rs1` | `0b000` | `0x01` | 把量子比特 `x[rs1]` 塌缩到 \|0⟩ |
| `qsample rd` | `0b000` | `0x02` | 测量全部比特，`x[rd]` 收到整数形式的结果 |
| `qh rs1` | `0b001` | `0x00` | H 作用在量子比特 `x[rs1]` |
| `qx rs1` | `0b001` | `0x01` | X |
| `qs rs1` | `0b001` | `0x02` | S |
| `qsdg rs1` | `0b001` | `0x03` | S† |
| `qt rs1` | `0b001` | `0x04` | T |
| `qtdg rs1` | `0b001` | `0x05` | T† |
| `qrz rs1, rs2` | `0b010` | `0x00` | Rz(`x[rs2]`·π/4096) 作用在 `x[rs1]` |
| `qry rs1, rs2` | `0b010` | `0x01` | Ry(`x[rs2]`·π/4096) 作用在 `x[rs1]` |
| `qcx rs1, rs2` | `0b011` | `0x00` | CNOT，控制 `x[rs1]`，目标 `x[rs2]` |
| `qswap rs1, rs2` | `0b011` | `0x01` | SWAP |
| `qcu1 rs1, rs2, rd` | `0b011` | `0x02` | CU1(`x[rd]`·π/4096)，控制 `x[rs1]`，目标 `x[rs2]` |
| `qccx rs1, rs2, rd` | `0b100` | `0x00` | Toffoli，控制 `x[rs1]`/`x[rs2]`，目标 `x[rd]` |
| `qmeas rd, rs1` | `0b101` | `0x00` | 测量量子比特 `x[rs1]`，`x[rd]` 收到 0 或 1，态塌缩 |

覆盖题面 12 门白名单的全部 12 个门，外加初始化、复位、测量与整体采样。
`qcu1` 和 `qccx` 借用 `rd` 字段承载第三个操作数——在 custom 空间里字段语义
由扩展自行定义，这是合法且常见的做法。

### 编码示例

```
qinit x28            →  0x000E000B     funct7=0x00 rs2=0  rs1=28 funct3=0b000 rd=0
qh    x28            →  0x000E100B     funct7=0x00 rs2=0  rs1=28 funct3=0b001 rd=0
qcx   x28, x29       →  0x01DE300B     funct7=0x00 rs2=29 rs1=28 funct3=0b011 rd=0
qmeas x10, x28       →  0x000E550B     funct7=0x00 rs2=0  rs1=28 funct3=0b101 rd=10
qccx  x28, x29, x30  →  0x01DE4F0B     funct7=0x00 rs2=29 rs1=28 funct3=0b100 rd=30
```

手工验算 `qcx x28, x29`：
`funct7=0000000`、`rs2=11101`、`rs1=11100`、`funct3=011`、`rd=00000`、`opcode=0001011`
→ `0000000 11101 11100 011 00000 0001011` = `0x01DE300B` ✓

---

## 4. 寄存器约定

统一编译器固定这样分配，两半永远不会撞车：

| 寄存器 | 用途 |
|---|---|
| `x0` | 硬连线 0（基础 ISA） |
| `x1` – `x9` | 经典变量 `r1`–`r9`（与 L3 契约一致） |
| `x10` – `x19` | 测量结果 `c[0]`, `c[1]`, …（由 `qmeas` 写入） |
| `x20` – `x27` | 经典代码生成器的临时寄存器 |
| `x28` – `x31` | 量子指令的比特下标与角度操作数 |

`x20`–`x31` 在程序末尾全部清零，理由和 L3 一样：
`execute()` 返回所有非零寄存器，终态里应该只剩程序真正的结果。

---

## 5. 两种等价的输入形式

模拟器同时接受助记符和原始编码字，二者执行完全一致：

```
    qh x28
    .word 0x000E100B        # 同一条指令
```

`python3 -m loomq qisa program.hqasm --words` 把整个程序改写成 `.word` 形式。
**这是编码不是装饰的证据**：测试会用两种形式跑同一个种子，要求终态逐位相同。

---

## 6. 端到端使用

```bash
cd starter_kit

# 编译成统一指令流
python3 -m loomq qisa examples/hybrid_bell.hqasm

# 看原始编码字
python3 -m loomq qisa examples/hybrid_bell.hqasm --words

# 编码/反汇编对照
python3 -m loomq qisa examples/hybrid_bell.hqasm --listing

# 真跑：随机种子多次执行，统计测量分布并与参考模拟器对照
python3 -m loomq qisa examples/hybrid_bell.hqasm --run --shots 2000

# 扩展模拟器自带的冒烟测试
python3 riscv_emulator_loomq.py

# 完整测试
python3 -m unittest tests.test_qisa -v
```

Python 里直接用：

```python
from loomq.qisa.compile import compile_unified
from riscv_emulator_loomq import QuantumRISCVEmulator

assembly = compile_unified(open("program.hqasm").read())

emulator = QuantumRISCVEmulator(seed=7)
emulator.load_program(assembly)
print(emulator.execute())      # {'x1': 105, 'x10': 1, 'x11': 1}
```

---

## 7. 实现说明

`riscv_emulator_loomq.py` **继承**官方 `TinyRISCVEmulator` 而不是复制它。
每一条 `li` / `add` / `sub` / `addi` / `beq` / `bne` / `j` 仍然走上游那份实现——
这个 fork 只**增加**指令，不重新解释任何一条已有指令，
所以 L3 评分依赖的经典语义逐字节不变。

量子态用 LoomQ 自己的态矢模拟器（`loomq/sim/statevector.py`），
`qmeas` 按真实边缘概率抽样并塌缩，随机源是构造函数里的 `seed`，
所以同一个种子可复现，不同种子给出真实的统计涨落。

上限 20 个量子比特（`MAX_QUBITS`），纯 Python 态矢，超出会明确报错。

---

## 8. 验证

`tests/test_qisa.py` 检查四件事：

1. **编码往返**：指令表里每一条，`encode` → `decode` 回到原样，操作数不错位；
2. **手工编码核对**：几条指令的 32 位字与手算结果逐位相同；
3. **两种形式等价**：助记符与 `.word` 形式在同一批种子下终态完全相同；
4. **端到端语义**：
   - 纠缠的两个比特在数百次执行里**永远同号**（不是"通常同号"）；
   - 经典分支的结果与那一次真实测量一致（`r1 = 105` 当且仅当 `c[0] = 1`）；
   - 用满 12 门白名单的电路，几千次执行的测量分布与
     `loomq/sim` 参考模拟器的精确分布做 Hellinger 保真度比对，要求 ≥ 0.97；
   - 临时寄存器已清零，终态只剩 `r1..r9` 与测量寄存器。
