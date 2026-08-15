from __future__ import annotations

import sys
from pathlib import Path
from generate_asm import CLASSES


def main(output):
    lines = ["#include <stddef.h>", "#include <stdint.h>",
        "typedef uint64_t (*kernel_fn)(uint64_t, uint64_t *);",
        "struct targetlab_descriptor { const char *metric; const char *category; kernel_fn kernel; };"]
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
            if left != right:
                entries.append((f"pairing.{right}.{left}", "pairing", symbol))
    entries.extend((
        ("branch.predictable", "branch", "targetlab_latency_branch"),
        ("branch.unpredictable", "branch", "targetlab_throughput_branch"),
        ("spills.load", "spill", "targetlab_latency_load"),
        ("spills.store", "spill", "targetlab_latency_store"),
    ))
    lines.append("const struct targetlab_descriptor targetlab_descriptors[] = {")
    lines.extend(f'  {{"{metric}", "{category}", {symbol}}},' for metric, category, symbol in entries)
    lines.extend(("};", "const size_t targetlab_descriptor_count = sizeof(targetlab_descriptors) / sizeof(targetlab_descriptors[0]);", ""))
    Path(output).write_text("\n".join(lines), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main(sys.argv[1])
