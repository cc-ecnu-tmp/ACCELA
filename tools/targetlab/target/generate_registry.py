from __future__ import annotations

import sys
from pathlib import Path
try:
    from .generate_asm import CLASSES
except ImportError:  # Direct script execution from make.
    from generate_asm import CLASSES


def main(output):
    lines = ["#include <stddef.h>", "#include <stdint.h>",
        "typedef uint64_t (*kernel_fn)(uint64_t, uint64_t *);",
        "struct targetlab_descriptor { const char *metric; const char *category; kernel_fn kernel; kernel_fn baseline; };"]
    entries = []
    for name in CLASSES:
        for mode in ("latency", "throughput"):
            symbol = f"targetlab_{mode}_{name}"
            lines.append(f"extern uint64_t {symbol}(uint64_t, uint64_t *);")
            entries.append((f"operations.{name}.{mode}", "arithmetic", symbol))
    for left_index, left in enumerate(CLASSES):
        for right in CLASSES[left_index:]:
            symbol = f"targetlab_pair_{left}_{right}"
            lines.append(f"extern uint64_t {symbol}(uint64_t, uint64_t *);")
            entries.append((f"pairing.{left}.{right}", "pairing", symbol))
    entries.extend((
        ("branch.predictable", "branch", "targetlab_latency_branch"),
        ("branch.unpredictable", "branch", "targetlab_branch_random"),
        ("spills.load", "spill", "targetlab_latency_load"),
        ("spills.store", "spill", "targetlab_latency_store"),
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
        entries.append((metric, category, symbol))
    lines.append("extern uint64_t targetlab_empty(uint64_t, uint64_t *);")
    lines.append("extern uint64_t targetlab_branch_random(uint64_t, uint64_t *);")
    lines.append("extern uint64_t targetlab_branch_random_base(uint64_t, uint64_t *);")
    lines.append("const struct targetlab_descriptor targetlab_descriptors[] = {")
    lines.extend(f'  {{"{metric}", "{category}", {symbol}, '
        f'{"targetlab_branch_random_base" if metric == "branch.unpredictable" else "targetlab_empty"}}},'
        for metric, category, symbol in entries)
    lines.extend(("};", "const size_t targetlab_descriptor_count = sizeof(targetlab_descriptors) / sizeof(targetlab_descriptors[0]);", ""))
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main(sys.argv[1])
