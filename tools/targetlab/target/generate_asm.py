from __future__ import annotations

import sys
from pathlib import Path

CLASSES = (
    "integer_alu", "integer_mul", "integer_div", "float_alu", "float_mul",
    "float_div", "load", "store", "branch", "call_return", "address", "move",
)
FLOAT_SETUP = "fmv.w.x ft0, t0\n  fmv.w.x ft1, t1\n  fmv.w.x ft2, t2\n  fmv.w.x ft3, t3"
OP = {"integer_alu": "addi t0, t0, 1", "integer_mul": "mul t0, t0, t1",
    "integer_div": "div t0, t0, t1", "float_alu": "fadd.s ft0, ft0, ft1",
    "float_mul": "fmul.s ft0, ft0, ft1", "float_div": "fdiv.s ft0, ft0, ft1",
    "load": "ld t0, 0(a1)", "store": "sd t0, 0(a1)",
    "branch": "beq t0, t1, 9f\n9:", "call_return": "call targetlab_leaf",
    "address": "lla t0, targetlab_anchor", "move": "mv t0, t1"}
OP2 = {"integer_alu": "addi t2, t2, 1", "integer_mul": "mul t2, t2, t3",
    "integer_div": "div t2, t2, t3", "float_alu": "fadd.s ft2, ft2, ft3",
    "float_mul": "fmul.s ft2, ft2, ft3", "float_div": "fdiv.s ft2, ft2, ft3",
    "load": "ld t2, 8(a1)", "store": "sd t2, 8(a1)",
    "branch": "beq t2, t3, 8f\n8:", "call_return": "call targetlab_leaf",
    "address": "lla t2, targetlab_anchor", "move": "mv t2, t3"}


def function(name, operations):
    setup = FLOAT_SETUP if any(item.startswith("float_") for item in operations) else ""
    body = [OP[item] if index == 0 else OP2[item] for index, item in enumerate(operations)]
    return f""".globl {name}
.type {name}, @function
{name}:
  addi sp, sp, -16
  sd ra, 8(sp)
  li t0, 1065353216
  li t1, 3
  li t2, 1065353216
  li t3, 5
  {setup}
1:
  {chr(10).join(body)}
  addi a0, a0, -1
  bnez a0, 1b
  xor a0, t0, t2
  ld ra, 8(sp)
  addi sp, sp, 16
  ret
.size {name}, .-{name}
"""


def memory_function(name, span_bytes, stride_bytes):
    mask = span_bytes - 1
    return f""".globl {name}
.type {name}, @function
{name}:
  mv a2, a1
  li t0, 0
  li t2, {mask}
1:
  and t1, t0, t2
  add t1, a2, t1
  ld t3, 0(t1)
  addi t0, t0, {stride_bytes}
  addi a0, a0, -1
  bnez a0, 1b
  mv a0, t3
  ret
.size {name}, .-{name}
"""


def repeated_function(name, instruction, repeat):
    body = "\n  ".join(instruction for _ in range(repeat))
    return f""".globl {name}
.type {name}, @function
{name}:
  li t0, 1
1:
  {body}
  addi a0, a0, -1
  bnez a0, 1b
  mv a0, t0
  ret
.size {name}, .-{name}
"""


def frontend_function(code_bytes):
    body = repeated_function(f"targetlab_frontend_{code_bytes}", "addi t0, t0, 1",
        max(1, code_bytes // 4 - 5))
    return ".option push\n.option norvc\n" + body + ".option pop\n"


def random_branch_function(name, with_branch):
    branch = "beqz t1, 2f\n2:" if with_branch else "nop"
    return f""".globl {name}
.type {name}, @function
{name}:
  li t0, 1
  li t2, 0
1:
  slli t1, t0, 13
  xor t0, t0, t1
  srli t1, t0, 7
  xor t0, t0, t1
  slli t1, t0, 17
  xor t0, t0, t1
  andi t1, t0, 1
  {branch}
  addi a0, a0, -1
  bnez a0, 1b
  xor a0, t0, t2
  ret
.size {name}, .-{name}
"""


def register_pressure_function(live_values):
    registers = ("t0", "t1", "t2", "t3", "t4", "t5", "t6", "a2", "a3", "a4", "a5", "a6",
        "a7", "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10")
    active = registers[:min(live_values, len(registers))]
    saved = tuple(register for register in active if register.startswith("s"))
    spills = max(0, live_values - len(registers))
    frame = ((len(saved) + spills) * 8 + 15) // 16 * 16
    lines = [f".globl targetlab_register_pressure_{live_values}",
        f".type targetlab_register_pressure_{live_values}, @function",
        f"targetlab_register_pressure_{live_values}:", f"  addi sp, sp, -{frame}"]
    for index, register in enumerate(saved):
        lines.append(f"  sd {register}, {index * 8}(sp)")
    spill_base = len(saved) * 8
    for index, register in enumerate(active):
        lines.append(f"  li {register}, {index + 1}")
    for index in range(spills):
        lines.extend((f"  li t0, {index + 1}", f"  sd t0, {spill_base + index * 8}(sp)"))
    lines.append("1:")
    lines.extend(f"  addi {register}, {register}, 1" for register in active)
    for index in range(spills):
        offset = spill_base + index * 8
        lines.extend((f"  ld t0, {offset}(sp)", "  addi t0, t0, 1", f"  sd t0, {offset}(sp)"))
    lines.extend(("  addi a0, a0, -1", "  bnez a0, 1b", f"  mv a0, {active[0]}"))
    lines.extend(f"  xor a0, a0, {register}" for register in active[1:])
    for index, register in enumerate(saved):
        lines.append(f"  ld {register}, {index * 8}(sp)")
    lines.extend((f"  addi sp, sp, {frame}", "  ret",
        f".size targetlab_register_pressure_{live_values}, .-targetlab_register_pressure_{live_values}", ""))
    return "\n".join(lines)


def main(output):
    lines = [".section .text", ".align 2", ".globl targetlab_leaf", "targetlab_leaf:", "  ret",
        ".globl targetlab_empty", "targetlab_empty:", "1:", "  addi a0, a0, -1", "  bnez a0, 1b", "  ret",
        ".section .rodata", ".align 3", "targetlab_anchor:", "  .dword 0", ".section .text"]
    for name in CLASSES:
        lines.append(function(f"targetlab_latency_{name}", (name,)))
        lines.append(function(f"targetlab_throughput_{name}", (name, name)))
    for left_index, left in enumerate(CLASSES):
        for right in CLASSES[left_index:]:
            lines.append(function(f"targetlab_pair_{left}_{right}", (left, right)))
    lines.extend((
        repeated_function("targetlab_load_use", "ld t0, 0(a1)\n  add t1, t1, t0", 1),
        repeated_function("targetlab_pointer_chase", "ld a1, 0(a1)", 1),
        random_branch_function("targetlab_branch_random", True),
        random_branch_function("targetlab_branch_random_base", False),
    ))
    for size in (4096, 32768, 262144):
        lines.append(memory_function(f"targetlab_working_set_{size}", size, 64))
    for stride in (8, 64, 512):
        lines.append(memory_function(f"targetlab_stride_{stride}", 262144, stride))
    for code_bytes in (64, 256, 1024):
        lines.append(frontend_function(code_bytes))
    for live_values in (8, 16, 24, 32):
        lines.append(register_pressure_function(live_values))
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: generate_asm.py OUTPUT")
    main(sys.argv[1])
