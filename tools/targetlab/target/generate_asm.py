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
    Path(output).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: generate_asm.py OUTPUT")
    main(sys.argv[1])
