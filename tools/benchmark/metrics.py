from __future__ import annotations

# Stable metric identifiers used by run-record.v1 and binary-analysis.v1.
# The analyzer may mark a metric unavailable, but it must never substitute zero.
ANALYZER_METRICS: dict[str, str] = {
    "elf_text_bytes": "bytes",
    "elf_rodata_bytes": "bytes",
    "elf_data_bytes": "bytes",
    "elf_bss_bytes": "bytes",
    "static_total_instructions": "instructions",
    "static_integer_instructions": "instructions",
    "static_floating_point_instructions": "instructions",
    "static_vector_instructions": "instructions",
    "static_branch_instructions": "instructions",
    "static_load_instructions": "instructions",
    "static_store_instructions": "instructions",
    "spill_count": "instructions",
    "reload_count": "instructions",
    "stack_frame_bytes": "bytes",
}

RUNTIME_METRICS: dict[str, str] = {
    "dynamic_instruction_count": "instructions",
    "dynamic_load_count": "instructions",
    "dynamic_store_count": "instructions",
    "l1d_miss_count": "misses",
}

UNAVAILABLE_REASONS = {
    "not_supported_by_toolchain",
    "not_emitted_by_compiler",
    "not_applicable",
    "not_collected_by_protocol",
    "remarks_not_available",
}


def rv64gc_qemu_v1() -> dict[str, object]:
    """Versioned formal proxy metric protocol shared by every pipeline profile."""

    additional: list[dict[str, str | None]] = [
        {
            "metric_id": "dynamic_load_count",
            "source": "file",
            "unit": "instructions",
            "pattern": r"(?m)^instructions=\d+\s+loads=(?P<value>\d+)\b",
        },
        {
            "metric_id": "dynamic_store_count",
            "source": "file",
            "unit": "instructions",
            "pattern": r"(?m)^instructions=\d+\s+loads=\d+\s+stores=(?P<value>\d+)\b",
        },
        {
            "metric_id": "l1d_miss_count",
            "source": "file",
            "unit": "misses",
            "pattern": r"(?m)^l1d=[^\r\n]*\bmisses=(?P<value>\d+)\b",
        },
        {"metric_id": "compile_time_ns", "source": "compile_time", "unit": "ns", "pattern": None},
        {"metric_id": "link_time_ns", "source": "link_time", "unit": "ns", "pattern": None},
        {"metric_id": "artifact_size_bytes", "source": "artifact_size", "unit": "bytes", "pattern": None},
        {"metric_id": "binary_size_bytes", "source": "binary_size", "unit": "bytes", "pattern": None},
    ]
    additional.extend(
        {"metric_id": metric_id, "source": "analyzer", "unit": unit, "pattern": None}
        for metric_id, unit in ANALYZER_METRICS.items()
    )
    return {
        "profile_id": "rv64gc-qemu-v1",
        "primary_metric_id": "dynamic_instruction_count",
        "metric_source": "file",
        "metric_unit": "instructions",
        "metric_pattern": r"(?m)^instructions=(?P<value>\d+)\b",
        "metric_file": "metrics.log",
        "analysis_file": "binary-analysis.json",
        "additional": additional,
    }


def cache_hotblock_metrics_v1() -> list[dict[str, str]]:
    """Normalized scalar evidence emitted by the dedicated three-plugin runner."""

    return [
        {
            "metric_id": "hotblock_hottest_address",
            "source": "file",
            "unit": "address",
            "pattern": r"(?m)^hotblock_rank=1 address=0x[0-9a-f]+ address_decimal=(?P<value>\d+) executions=\d+ instructions=\d+ dynamic=\d+$",
        },
        {
            "metric_id": "hotblock_hottest_executions",
            "source": "file",
            "unit": "executions",
            "pattern": r"(?m)^hotblock_rank=1 address=0x[0-9a-f]+ address_decimal=\d+ executions=(?P<value>\d+) instructions=\d+ dynamic=\d+$",
        },
        {
            "metric_id": "hotblock_hottest_dynamic_instructions",
            "source": "file",
            "unit": "instructions",
            "pattern": r"(?m)^hotblock_rank=1 address=0x[0-9a-f]+ address_decimal=\d+ executions=\d+ instructions=\d+ dynamic=(?P<value>\d+)$",
        },
    ]
