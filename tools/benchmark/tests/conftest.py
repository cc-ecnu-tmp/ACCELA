from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tools.benchmark.adapters import StageSpec
from tools.benchmark.execution import MeasurementSpec, RunOptions, RunProvenance, ToolVersion
from tools.benchmark.inventory import inventory_suite
from tools.benchmark.protocol import capture_measurement_protocol, REQUIRED_ASSETS
from tools.benchmark.util import atomic_write_json
from tools.benchmark.util import sha256_file, sha256_json


TOOL_SOURCE = r'''
from __future__ import annotations
import os
import json
import shutil
import sys
import time

mode = sys.argv[1]
if mode == "compile":
    shutil.copyfile(sys.argv[2], sys.argv[3])
    sys.stderr.write("static=7 spills=0\n")
elif mode == "compile-sampled":
    sample = int(sys.argv[4])
    sys.stderr.write(f"compile-sample={sample}\n")
    if sample == 2:
        raise SystemExit(7)
    shutil.copyfile(sys.argv[2], sys.argv[3])
elif mode == "run":
    behavior = sys.argv[3] if len(sys.argv) > 3 else "exact"
    if behavior == "sleep":
        time.sleep(5)
    data = sys.stdin.buffer.read()
    if behavior == "append":
        data += b"\n"
    if behavior == "result-file":
        with open(sys.argv[4], "wb") as stream:
            stream.write(b"0\n")
        sys.stdout.buffer.write(data)
    elif behavior == "process-exit":
        sys.stdout.buffer.write(data)
    elif behavior == "return-one":
        sys.stdout.buffer.write(data + b"1\n")
    else:
        sys.stdout.buffer.write(data + b"0\n")
    sys.stdout.buffer.flush()
    metric = os.environ.get("TEST_INSTRUCTIONS", "80")
    if behavior == "vary":
        metric = str(int(metric) + int(sys.argv[4]))
    if behavior == "file":
        metric_path = sys.argv[4]
        stale = os.path.exists(metric_path)
        with open(metric_path, "a", encoding="utf-8") as stream:
            stream.write(f"profile instructions={'999' if stale else metric} loads=30 stores=12\n")
            stream.write("cache l1d=4\n")
    else:
        sys.stderr.write(f"instructions={metric} loads=30 stores=12 l1d=4\n")
elif mode == "analyze":
    measurements = []
    measured = {
        "elf_text_bytes": (101, "bytes"),
        "elf_rodata_bytes": (20, "bytes"),
        "elf_data_bytes": (8, "bytes"),
        "static_integer_instructions": (12, "instructions"),
        "static_branch_instructions": (3, "instructions"),
        "static_load_instructions": (4, "instructions"),
        "static_store_instructions": (2, "instructions"),
        "stack_frame_bytes": (32, "bytes"),
    }
    for metric_id, (value, unit) in measured.items():
        measurements.append({"metric_id": metric_id, "value": value, "unit": unit, "availability": "measured", "reason": None})
    for metric_id in ("spill_count", "reload_count"):
        measurements.append({"metric_id": metric_id, "value": None, "unit": "instructions", "availability": "unavailable", "reason": "not_supported_by_toolchain"})
    with open(sys.argv[3], "w", encoding="utf-8", newline="\n") as stream:
        json.dump({"schema_version": "binary-analysis.v1", "measurements": measurements}, stream)
        stream.write("\n")
else:
    raise SystemExit(64)
'''


@pytest.fixture
def benchmark_fixture(tmp_path: Path):
    suite = tmp_path / "suite"
    suite.mkdir()
    for index in range(10):
        stem = f"family{index // 2 + 1}_{index + 1}"
        (suite / f"{stem}.sy").write_text(f"source {index}\n", encoding="utf-8", newline="\n")
        payload = f"input-{index}\n\n".encode()
        (suite / f"{stem}.in").write_bytes(payload)
        (suite / f"{stem}.out").write_bytes(payload + b"0\n")
    manifest = inventory_suite(
        suite,
        suite_id="fixture-suite",
        target="rv64gc",
        data_role="B3",
        origin_source="fixture-snapshot",
        origin_snapshot_sha256="a" * 64,
        license_expression="NOASSERTION",
    )
    manifest_path = tmp_path / "manifest.json"
    atomic_write_json(manifest_path, manifest)
    tool = tmp_path / "fixture_tool.py"
    tool.write_text(TOOL_SOURCE, encoding="utf-8", newline="\n")
    def options(
        *,
        output_name: str = "run.json",
        behavior: str = "exact",
        primary_metric_id: str = "dynamic_instruction_count",
        metric_source: str = "stderr",
        metric_pattern: str | None = r"instructions=(?P<value>\d+)",
        metric_unit: str = "instructions",
        run_timeout: float = 2.0,
        run_id: str = "fixture-run",
        runner_environment: dict[str, str] | None = None,
        additional_metrics: tuple[MeasurementSpec, ...] = (),
        compile_repetitions: int = 1,
    ) -> RunOptions:
        runner = StageSpec(
            "qemu",
            "host",
            (
                sys.executable,
                "{runner_executable}",
                "run",
                "{binary}",
                behavior,
                "{qemu_binary}",
                "{profile_plugin_binary}",
                "{cache_plugin_binary}",
            ),
            runner_environment or {},
        )
        protocol_assets = {
            key: (Path(sys.executable) if key == "qemu_binary" else tool)
            for key in REQUIRED_ASSETS
        }
        protocol = capture_measurement_protocol(
            protocol_id="fixture-rv64gc-qemu",
            assets=protocol_assets,
            runner=runner,
            machine="virt",
            cpu_model="rv64",
            memory="128M",
        )
        protocol_path = tmp_path / f"measurement-protocol-{sha256_json(protocol)[:12]}.json"
        atomic_write_json(protocol_path, protocol)
        return RunOptions(
            manifest_path=manifest_path,
            suite_root=suite,
            workspace_root=tmp_path,
            output_path=tmp_path / output_name,
            state_root=tmp_path / "state",
            compiler=StageSpec(
                "benchmark-compiler",
                "host",
                (sys.executable, str(tool), "compile", "{source}", "{artifact}"),
                {},
            ),
            linker=None,
            runner=runner,
            provenance=RunProvenance(
                repo_commit="1" * 40,
                repo_dirty=False,
                pipeline_profile_id="fixture-profile",
                pipeline_profile_sha256="b" * 64,
                compiler_artifact_sha256=sha256_file(tool),
                measurement_protocol_id=protocol["protocol_id"],
                measurement_protocol_sha256=sha256_json(protocol),
            ),
            measurement_protocol_path=protocol_path,
            measurement_protocol_assets=tuple(protocol_assets.items()),
            compile_timeout_seconds=2,
            compile_repetitions=compile_repetitions,
            run_timeout_seconds=run_timeout,
            repetitions=1,
            max_workers=2,
            keep_going=True,
            primary_metric_id=primary_metric_id,
            metric_source=metric_source,
            metric_pattern=metric_pattern,
            metric_unit=metric_unit,
            additional_metrics=additional_metrics,
            run_id=run_id,
            environment_label="local_reference",
            tool_versions=(ToolVersion("riscv-gcc", "13.2", "13.3"), ToolVersion("clang", "18.1.3", "18.1.3")),
        )

    return tmp_path, suite, manifest_path, tool, options
