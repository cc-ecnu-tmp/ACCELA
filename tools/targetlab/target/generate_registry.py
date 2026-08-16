from __future__ import annotations

import sys
from pathlib import Path
try:
    from .generate_asm import CLASSES, CORE_UNROLL
except ImportError:  # Direct script execution from make.
    from generate_asm import CLASSES, CORE_UNROLL


def main(output, qemu_proxy=False):
    lines = ["#include <stddef.h>", "#include <stdint.h>",
        "typedef uint64_t (*kernel_fn)(uint64_t, uint64_t *);",
        "struct targetlab_descriptor { const char *metric; const char *category; kernel_fn kernel; kernel_fn baseline; uint64_t normalization; };"]
    entries = []
    for name in CLASSES:
        for mode in ("latency", "throughput"):
            measured_name = "integer_alu" if qemu_proxy and name == "move" else name
            symbol = f"targetlab_{mode}_{measured_name}"
            lines.append(f"extern uint64_t {symbol}(uint64_t, uint64_t *);")
            entries.append((f"operations.{name}.{mode}", "arithmetic", symbol,
                CORE_UNROLL * 2 if mode == "throughput" else CORE_UNROLL))
    for left_index, left in enumerate(CLASSES):
        for right in CLASSES[left_index:]:
            measured_left = "integer_alu" if qemu_proxy and left == "move" else left
            measured_right = "integer_alu" if qemu_proxy and right == "move" else right
            left_position = CLASSES.index(measured_left)
            right_position = CLASSES.index(measured_right)
            if left_position > right_position:
                measured_left, measured_right = measured_right, measured_left
            symbol = f"targetlab_pair_{measured_left}_{measured_right}"
            lines.append(f"extern uint64_t {symbol}(uint64_t, uint64_t *);")
            entries.append((f"pairing.{left}.{right}", "pairing", symbol, CORE_UNROLL))
    entries.extend((
        ("branch.predictable", "branch", "targetlab_latency_branch", CORE_UNROLL),
        ("branch.unpredictable", "branch", "targetlab_branch_random", CORE_UNROLL),
        ("spills.load", "spill", "targetlab_latency_load", CORE_UNROLL),
        ("spills.store", "spill", "targetlab_latency_store", CORE_UNROLL),
    ))
    diagnostics = [
        ("diagnostics.load_use", "memory", "targetlab_load_use"),
        ("diagnostics.pointer_chase", "memory", "targetlab_pointer_chase"),
    ]
    diagnostics.extend((f"diagnostics.working_set.{size}", "working_set",
        f"targetlab_working_set_{size}") for size in (4096, 32768, 262144))
    diagnostics.extend((f"diagnostics.stride.{stride}", "stride",
        f"targetlab_stride_{stride}") for stride in (8, 64, 512))
    diagnostics.extend((f"diagnostics.frontend.{size}", "frontend",
        f"targetlab_frontend_{size}") for size in (64, 256, 1024))
    diagnostics.extend((f"diagnostics.register_pressure.{count}", "register_pressure",
        f"targetlab_register_pressure_{count}") for count in (8, 16, 24, 32))
    for metric, category, symbol in diagnostics:
        lines.append(f"extern uint64_t {symbol}(uint64_t, uint64_t *);")
        entries.append((metric, category, symbol, 1))
    lines.append("extern uint64_t targetlab_empty(uint64_t, uint64_t *);")
    lines.append("extern uint64_t targetlab_branch_random(uint64_t, uint64_t *);")
    lines.append("extern uint64_t targetlab_branch_random_base(uint64_t, uint64_t *);")
    lines.append("const struct targetlab_descriptor targetlab_descriptors[] = {")
    lines.extend(f'  {{"{metric}", "{category}", {symbol}, '
        f'{"targetlab_branch_random_base" if metric == "branch.unpredictable" else "targetlab_empty"}, {normalization}}},'
        for metric, category, symbol, normalization in entries)
    lines.extend(("};", "const size_t targetlab_descriptor_count = sizeof(targetlab_descriptors) / sizeof(targetlab_descriptors[0]);", ""))
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    if len(sys.argv) not in {2, 3} or (len(sys.argv) == 3 and sys.argv[2] != "--qemu-proxy"):
        raise SystemExit("usage: generate_registry.py OUTPUT [--qemu-proxy]")
    main(sys.argv[1], qemu_proxy=len(sys.argv) == 3)
