from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tools.benchmark import fast_plan
from tools.benchmark.cli import build_parser
from tools.benchmark.tests.test_stats_ablation_report import make_run
from tools.benchmark.util import read_json, sha256_file, sha256_json


NOW = "2026-08-13T00:00:00Z"
CANDIDATES = [f"candidate.{letter}" for letter in "abcdef"]
ORACLE_STATIC_FILES = {
    "candidate-screening": "candidate-screening.v1.json",
    "candidate-oracle-capture": "candidate-oracle-capture.v1.json",
    "candidate-evidence": "candidate-evidence.v1.json",
    "candidate-screening-spec": "candidate-screening-spec.v1.json",
}


def _write(path: Path, document: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _artifact(root: Path, path: Path) -> dict[str, str]:
    document = read_json(path) if path.suffix == ".json" else None
    return {
        "path": path.relative_to(root).as_posix(),
        "canonical_sha256": sha256_json(document) if document is not None else sha256_file(path),
        "physical_sha256": sha256_file(path),
    }


def _fixture(root: Path) -> tuple[Path, Path, Path]:
    compiler_source = root / "inputs/compiler-source.bin"
    compiler_source.parent.mkdir(parents=True, exist_ok=True)
    compiler_source.write_bytes(b"source-compiler")
    static: dict[str, dict[str, str]] = {}
    for name in (
        "candidate_registry",
        "executable_pass_registry",
        "screening_base_pass_registry",
    ):
        path = _write(root / f"inputs/{name}.json", {"name": name})
        static[name] = _artifact(root, path)
    oracle_bindings = []
    repository_root = Path(__file__).resolve().parents[3]
    for artifact_id, filename in ORACLE_STATIC_FILES.items():
        source_path = (
            repository_root / "docs/optimization/data/candidates" / filename
        )
        destination = root / "docs/optimization/data/candidates" / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination)
        row = {"artifact_id": artifact_id, "artifact": _artifact(root, destination)}
        row["verification_commitment_sha256"] = sha256_json(row)
        oracle_bindings.append(row)
    static["screening"] = next(
        row["artifact"]
        for row in oracle_bindings
        if row["artifact_id"] == "candidate-screening"
    )

    profile_rows = []
    for selected in ([], *[[item] for item in CANDIDATES]):
        profile_id = "candidate-empty" if not selected else f"full+{selected[0]}"
        profile = {
            "schema_version": 2,
            "base": "FULL",
            "disable": [],
            "enable_candidates": selected,
        }
        path = _write(root / f"profiles/{profile_id}.json", profile)
        profile_rows.append(
            {
                "profile_id": profile_id,
                "kind": "candidate_empty" if not selected else "single",
                "candidate_ids": selected,
                "enabled_candidate_ids": selected,
                "profile_sha256": sha256_file(path),
                "path": path.relative_to(root).as_posix(),
            }
        )
    matrix_path = _write(
        root / "inputs/matrix.json",
        {"schema_version": "candidate-profile-matrix.v1", "profiles": profile_rows},
    )
    static["matrix"] = _artifact(root, matrix_path)
    snapshot_path = _write(root / "inputs/toolchain.json", {"snapshot": True})
    snapshot = _artifact(root, snapshot_path)
    protocols = {}
    for mode in ("standard_proxy", "cache_hotblock"):
        path = _write(root / f"inputs/{mode}.json", {"mode": mode})
        artifact = _artifact(root, path)
        protocols[mode] = {
            "path": artifact["path"],
            "protocol_sha256": artifact["canonical_sha256"],
            "physical_sha256": artifact["physical_sha256"],
            "protocol_id": mode,
            "runner_command_sha256": fast_plan._stage(
                "qemu",
                ["sh", "{runner_executable}", "{binary}", "{metric_file}", "{input}"],
                environment={
                    "QEMU_SYSTEM_RISCV64": "{qemu_binary}",
                    "QEMU_PROFILE_PLUGIN": "{profile_plugin_binary}",
                    "QEMU_CACHE_PLUGIN": "{cache_plugin_binary}",
                    **(
                        {"QEMU_HOTBLOCK_PLUGIN": "{hotblocks_plugin_binary}"}
                        if mode == "cache_hotblock"
                        else {}
                    ),
                },
            )["command_sha256"],
        }
    suites = []
    for role, count in zip(("B1", "B2", "B3", "B4", "B5", "B6"), (140, 20, 60, 59, 60, 88)):
        path = _write(root / f"inputs/{role}.json", {"role": role})
        suites.append(
            {
                "data_role": role,
                "suite_id": f"suite-{role}",
                "case_count": count,
                "manifest": _artifact(root, path),
            }
        )
    source = {
        "schema_version": "candidate-campaign-plan.v1",
        "repository": {
            "repo_commit": "0" * 40,
            "repo_tree": "3" * 40,
            "compiler_artifact": {
                "path": "inputs/compiler-source.bin",
                "physical_sha256": sha256_file(compiler_source),
            },
        },
        "qualified_candidate_ids": CANDIDATES,
        "execution_environment_sha256": "e" * 64,
        "artifacts": static,
        "reference_toolchain": {
            "snapshot": snapshot,
            "common_tool_versions": {
                "qemu-system-riscv64": "11.0.3",
                "bare-metal-linker": "15.2.0",
                "python": "3.14.6",
                "glib": "2.88.3",
            },
            "accela_jdk_version": "21.0.11",
            "baselines": [
                {
                    "profile_id": "gcc-13.3-o2", "profile_sha256": "4" * 64,
                    "tool": "riscv-gcc", "version": "13.3.0",
                },
                {
                    "profile_id": "clang-18-o3", "profile_sha256": "5" * 64,
                    "tool": "clang", "version": "18.1.3",
                },
            ],
        },
        "measurement_protocols": protocols,
        "suites": suites,
    }
    source_path = _write(root / "control/source.json", source)

    imported = []
    for task_id in ["run.B1.full", *[f"run.B1.{item}" for item in CANDIDATES], "run.B2.full"]:
        imported_run: dict[str, object] = {"task_id": task_id}
        if task_id == "run.B2.full":
            seed = make_run(
                "source:run.B2.full",
                {"case": ("family", 1.0)},
                profile_id="candidate-empty",
                profile_sha256="a" * 64,
            )
            seed["state"] = "completed"
            seed["provenance"]["execution_environment_sha256"] = "e" * 64
            imported_run = seed
        run_path = _write(root / f"imports/{task_id}.json", imported_run)
        row = {
            "task_id": task_id,
            "run_id": f"source:{task_id}",
            "run_artifact": _artifact(root, run_path),
            "terminal_commitment_sha256": sha256_json({"task_id": task_id}),
        }
        row["verification_commitment_sha256"] = sha256_json(row)
        imported.append(row)
    source_binding = {
        "artifact_id": "source-plan",
        "artifact": _artifact(root, source_path),
    }
    source_binding["verification_commitment_sha256"] = sha256_json(source_binding)
    static_binding = {
        "artifact_id": "static-matrix",
        "artifact": static["matrix"],
    }
    static_binding["verification_commitment_sha256"] = sha256_json(static_binding)
    measurement_bindings = []
    for artifact_id in (
        "profile_plugin_source", "cache_plugin_source", "hotblocks_plugin_source",
        "runtime_filter_source", "runtime_source", "crt_source", "linker_script_source",
        "profile_plugin_binary", "cache_plugin_binary", "hotblocks_plugin_binary",
        "qemu_binary", "runner_executable_standard", "runner_executable_cache",
    ):
        asset_path = root / f"assets/{artifact_id}.bin"
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_bytes(artifact_id.encode())
        row = {"artifact_id": artifact_id, "artifact": _artifact(root, asset_path)}
        row["verification_commitment_sha256"] = sha256_json(row)
        measurement_bindings.append(row)
    bootstrap: dict[str, object] = {
        "schema_version": "candidate-fast-bootstrap.v1",
        "bootstrap_id": "bootstrap",
        "campaign_id": "fast",
        "created_at": NOW,
        "source_revision": {"commit": "0" * 40, "tree": "3" * 40, "dirty": False},
        "evaluation_revision": {"commit": "1" * 40, "tree": "2" * 40, "dirty": False},
        "source_artifacts": [source_binding],
        "static_artifacts": [
            static_binding,
            *oracle_bindings,
            *measurement_bindings,
        ],
        "imported_receipts": imported,
        "bootstrap_commitment_sha256": "0" * 64,
    }
    bootstrap["bootstrap_commitment_sha256"] = sha256_json(
        {key: value for key, value in bootstrap.items() if key != "bootstrap_commitment_sha256"}
    )
    bootstrap_path = _write(root / "control/bootstrap.json", bootstrap)

    compiler = root / "inputs/compiler.bin"
    compiler.write_bytes(b"compiler")
    compiler_artifact = _artifact(root, compiler)
    blueprint_rows = []
    for selector in sorted(fast_plan._RUN_SELECTORS):
        seed_run = make_run(
            f"seed:{selector}",
            {"case": ("family", 1.0)},
            profile_id="candidate-empty",
            profile_sha256="a" * 64,
        )
        configuration = seed_run["configuration"]
        configuration.update(
            {
                "pipeline_profile_file_sha256": "a" * 64,
                "candidate_registry_sha256": "b" * 64,
                "candidate_pass_registry_sha256": "c" * 64,
                "enabled_candidate_ids": [],
                "compile_timeout_seconds": 120.0,
                "compile_repetitions": 5,
                "reuse_compile_cache": False,
                "compile_storage_contract": "attempt_local_v1",
                "link_timeout_seconds": 120.0,
                "analyze_timeout_seconds": 120.0,
                "run_timeout_seconds": 1800.0,
                "timeout_policy": "initial",
                "baseline_timeout_run_sha256": None,
                "baseline_timeout_run_id": None,
                "timeout_minimum_seconds": 120.0,
                "timeout_multiplier": 3.0,
                "timeout_cap_seconds": 1800.0,
                "repetitions": 1,
                "max_workers": 4,
                "keep_going": False,
                "retry_failures": False,
                "seed": 20260809,
                "artifact_suffix": ".s",
                "binary_suffix": ".elf",
                "environment_label": "proxy",
                "evidence_level": "qemu_proxy",
                "metric_profile_id": "rv64gc-qemu-v1",
                "primary_metric_id": "dynamic_instruction_count",
                "consistency_fraction": 0.1,
                "consistency_repetitions": 3,
                "tool_versions": [
                    {
                        "tool": "python",
                        "actual": "3.13",
                        "official_expected": "3.13",
                        "comparison": "exact",
                    }
                ],
            }
        )
        provenance = seed_run["provenance"]
        provenance["execution_environment_sha256"] = "e" * 64
        selected_artifact = (
            snapshot if selector.startswith("reference-") else compiler_artifact
        )
        provenance["compiler_artifact_sha256"] = selected_artifact[
            "physical_sha256"
        ]
        blueprint_rows.append(
            {
                "selector": selector,
                "compiler_artifact": selected_artifact,
                "argv_tail": [
                    "--compiler-command-json", '["sh","compile.sh"]',
                    "--link-command-json", '["sh","link.sh"]',
                    "--analyzer-command-json", '["sh","analyze.sh"]',
                    "--runner-command-json", '["sh","run.sh"]',
                    "--metric-file", "metric.json",
                    "--analysis-file", "analysis.json",
                    "--remarks-file", "remarks.jsonl",
                    "--measurement-asset", "QEMU_SYSTEM_RISCV64=inputs/qemu",
                    "--tool-version", "python=3.13",
                    "--official-version", "python=3.13",
                ],
                "configuration": configuration,
                "provenance": provenance,
            }
        )
    blueprint_document: dict[str, object] = {
        "schema_version": "candidate-fast-launch-blueprints.v1",
        "blueprints": blueprint_rows,
        "blueprint_commitment_sha256": "0" * 64,
    }
    blueprint_document["blueprint_commitment_sha256"] = sha256_json(
        {
            key: value
            for key, value in blueprint_document.items()
            if key != "blueprint_commitment_sha256"
        }
    )
    blueprints_path = _write(
        root / "control/blueprints.json",
        blueprint_document,
    )
    return bootstrap_path, source_path, blueprints_path


def test_factory_derives_complete_parallel_dag_without_raw_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap, source, _ = _fixture(tmp_path)
    monkeypatch.setattr(fast_plan, "load_and_validate", read_json)
    fast_plan.build_fast_launch_blueprints(
        workspace_root=tmp_path,
        bootstrap_path=bootstrap,
        source_plan_path=source,
        output_path=Path("control/production-blueprints.json"),
    )
    blueprints = tmp_path / "control/production-blueprints.json"

    plan, templates = fast_plan.build_fast_plan_factory(
        workspace_root=tmp_path,
        bootstrap_path=bootstrap,
        source_plan_path=source,
        blueprint_path=blueprints,
        plan_id="fast-plan",
        plan_output_path=Path("control/plan.json"),
        launch_template_output_path=Path("control/launch-templates.json"),
        campaign_output_root=Path("fast-output"),
        campaign_state_root=Path("fast-state"),
        diagnostic_profile_root=Path("fast-output/diagnostic-profiles"),
        created_at=NOW,
    )

    tasks = {row["task_id"]: row for row in plan["tasks"]}
    assert plan["max_parallel_runs"] == plan["jobs_per_run"] == 4
    assert len([key for key in tasks if key.startswith("run.B2.candidate.")]) == 6
    assert tasks["run.B3.full"]["dependencies"] == ["audit.B2"]
    assert tasks["run.B3.gcc"]["dependencies"] == ["audit.B2"]
    assert tasks["run.B3.clang"]["dependencies"] == ["audit.B2"]
    for task_id, expected_hash in (
        ("run.B3.gcc", "4" * 64),
        ("run.B3.clang", "5" * 64),
    ):
        task = tasks[task_id]
        assert task["profile"] is None
        assert task["reference_profile_sha256"] == expected_hash
        assert task["compiler_artifact"] == _artifact(
            tmp_path, tmp_path / "inputs/toolchain.json"
        )
        argv = next(row["argv"] for row in templates["tasks"] if row["task_id"] == task_id)
        assert "--pipeline-profile-file" not in argv
        assert "--candidate-registry" not in argv
        assert "--candidate-pass-registry" not in argv
    for candidate in CANDIDATES:
        assert tasks[f"run.B3.{candidate}"]["dependencies"] == ["run.B3.full"]
    for role in ("B4", "B5", "B6"):
        assert tasks[f"run.{role}.full"]["dependencies"] == ["audit.B3"]
        for candidate in CANDIDATES:
            assert tasks[f"run.{role}.{candidate}"]["dependencies"] == [
                "audit.B3", f"run.{role}.full"
            ]
    pairs = [row for row in tasks.values() if row["task_id"].startswith("diagnostic.pair.")]
    assert len(pairs) == 15
    assert all(row["gate"] == "diagnostic_top3" for row in pairs)
    caches = [row for row in tasks.values() if row["task_id"].startswith("diagnostic.cache.")]
    assert len(caches) == 7
    assert tasks["study.diagnostic"]["terminal_dependencies"] == [
        row["task_id"] for row in plan["tasks"] if row["kind"] == "diagnostic"
    ]
    assert tasks["final"]["dependencies"] == ["audit.final", "study.diagnostic"]
    assert tasks["final"]["terminal_dependencies"] == ["study.B4", "study.B5", "study.B6"]

    assert len(templates["tasks"]) == len(
        [row for row in plan["tasks"] if row["kind"] in {"run", "diagnostic"}]
    )
    for row in templates["tasks"]:
        argv = row["argv"]
        assert argv[0] == "{python}"
        assert argv[argv.index("--jobs") + 1] == "4"
        assert "--candidate-status-ledger" not in argv
        assert "--candidate-campaign-plan" not in argv
        assert not any("raw" in item.lower() or "ledger" in item.lower() for item in argv)
    assert all(
        binding["artifact"]["path"] != "" and not Path(binding["artifact"]["path"]).is_absolute()
        for task in plan["tasks"]
        for binding in task["static_bindings"]
    )


def test_blueprint_producer_derives_all_formal_selectors_from_b2_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap, source, _ = _fixture(tmp_path)
    monkeypatch.setattr(fast_plan, "load_and_validate", read_json)
    document = fast_plan.build_fast_launch_blueprints(
        workspace_root=tmp_path,
        bootstrap_path=bootstrap,
        source_plan_path=source,
        output_path=Path("control/derived-blueprints.json"),
    )
    assert [row["selector"] for row in document["blueprints"]] == sorted(
        fast_plan._RUN_SELECTORS
    )
    assert document["blueprint_commitment_sha256"] == sha256_json(
        {
            key: value
            for key, value in document.items()
            if key != "blueprint_commitment_sha256"
        }
    )
    by_selector = {row["selector"]: row for row in document["blueprints"]}
    for selector, row in by_selector.items():
        configuration = row["configuration"]
        assert configuration["max_workers"] == 4
        assert configuration["compile_repetitions"] == 5
        assert configuration["reuse_compile_cache"] is False
        assert configuration["timeout_policy"] == "initial"
        assert all(item["comparison"] == "exact" for item in configuration["tool_versions"])
        assert "--candidate-fast-plan" not in row["argv_tail"]
        assert "--candidate-status-ledger" not in row["argv_tail"]
        if selector.startswith("reference-"):
            assert row["compiler_artifact"] == _artifact(
                tmp_path, tmp_path / "inputs/toolchain.json"
            )
            assert "{profile}" not in " ".join(row["argv_tail"])
            assert configuration["compiler"]["kind"] == "external"
        else:
            assert configuration["compiler"]["kind"] == "benchmark-compiler"
    assert len(
        [
            item
            for item in by_selector["accela-cache"]["argv_tail"]
            if item.startswith("QEMU_HOTBLOCK_PLUGIN=")
        ]
    ) == 1

    tampered = read_json(tmp_path / "control/derived-blueprints.json")
    tampered["blueprints"][0]["configuration"]["max_workers"] = 8
    tampered["blueprint_commitment_sha256"] = sha256_json(
        {
            key: value
            for key, value in tampered.items()
            if key != "blueprint_commitment_sha256"
        }
    )
    tampered_path = _write(tmp_path / "control/derived-blueprints-tampered.json", tampered)
    with pytest.raises(Exception, match="fixed formal run contract"):
        fast_plan._load_blueprints(tmp_path, tampered_path)
    asset = tmp_path / "assets/profile_plugin_binary.bin"
    asset.write_bytes(asset.read_bytes() + b"tampered")
    with pytest.raises(Exception, match="hash binding differs"):
        fast_plan.build_fast_launch_blueprints(
            workspace_root=tmp_path,
            bootstrap_path=bootstrap,
            source_plan_path=source,
            output_path=Path("control/never-published-blueprints.json"),
        )
    assert not (tmp_path / "control/never-published-blueprints.json").exists()


def test_blueprints_reject_task_controlled_argv(tmp_path: Path) -> None:
    _, _, blueprint_path = _fixture(tmp_path)
    document = read_json(blueprint_path)
    document["blueprints"][0]["argv_tail"].extend(["--jobs", "8"])
    document["blueprint_commitment_sha256"] = sha256_json(
        {
            key: value
            for key, value in document.items()
            if key != "blueprint_commitment_sha256"
        }
    )
    blueprint_path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    with pytest.raises(Exception, match="controlled arguments"):
        fast_plan._load_blueprints(tmp_path, blueprint_path)


def test_cli_registers_factory_and_materializer() -> None:
    parser = build_parser()
    factory = parser.parse_args(
        [
            "candidates", "fast-plan-factory", "--workspace-root", ".",
            "--bootstrap", "b.json", "--source-plan", "s.json", "--blueprints", "l.json",
            "--plan-id", "p", "--campaign-output-root", "out", "--campaign-state-root", "state",
            "--diagnostic-profile-root", "profiles", "--output-plan", "plan.json",
            "--output-launch-templates", "launch.json",
        ]
    )
    assert factory.candidates_command == "fast-plan-factory"
    materialize = parser.parse_args(
        [
            "candidates", "fast-materialize-launch", "--workspace-root", ".",
            "--templates", "launch.json", "--head", "head.json", "--output", "wave.json",
        ]
    )
    assert materialize.candidates_command == "fast-materialize-launch"
    blueprints = parser.parse_args(
        [
            "candidates", "fast-launch-blueprints", "--workspace-root", ".",
            "--bootstrap", "bootstrap.json", "--source-plan", "source.json",
            "--output", "blueprints.json",
        ]
    )
    assert blueprints.candidates_command == "fast-launch-blueprints"


def test_materializer_resolves_imported_and_index_baselines_and_rejects_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fast_plan, "load_and_validate", read_json)
    imported_run = _write(tmp_path / "runs/imported.json", {"run_id": "imported"})
    dynamic_run = _write(tmp_path / "runs/dynamic.json", {"run_id": "dynamic"})
    receipt = _write(
        tmp_path / "receipts/dynamic.json",
        {
            "terminal": {"state": "completed"},
            "run_artifact": _artifact(tmp_path, dynamic_run),
        },
    )
    tasks = [
        {"task_id": "run.imported", "kind": "run", "expected_configuration_template_sha256": "a" * 64},
        {"task_id": "run.dynamic", "kind": "diagnostic", "expected_configuration_template_sha256": "b" * 64},
    ]
    plan = {"schema_version": "candidate-fast-campaign-plan.v1", "tasks": tasks}
    plan_path = _write(tmp_path / "control/plan.json", plan)
    index = {
        "schema_version": "candidate-fast-run-index.v1",
        "receipts": [{"task_id": "baseline.dynamic", "receipt": _artifact(tmp_path, receipt)}],
    }
    index_path = _write(tmp_path / "control/index.json", index)
    status = {
        "schema_version": "candidate-fast-status.v1",
        "plan": _artifact(tmp_path, plan_path),
        "ready_tasks": ["run.imported", "run.dynamic"],
    }
    status_path = _write(tmp_path / "control/status.json", status)
    head = {
        "schema_version": "candidate-fast-current-head.v1",
        "campaign_id": "fast",
        "status": _artifact(tmp_path, status_path),
        "index": _artifact(tmp_path, index_path),
    }
    head_path = _write(tmp_path / "control/head.json", head)
    launch_tasks = [
        {
            "task_id": "run.imported",
            "baseline_task_id": "baseline.imported",
            "baseline_artifact": _artifact(tmp_path, imported_run),
            "configuration_template_sha256": "a" * 64,
            "argv": ["{python}", "--baseline-timeout-run", "{baseline_run_path}"],
        },
        {
            "task_id": "run.dynamic",
            "baseline_task_id": "baseline.dynamic",
            "baseline_artifact": None,
            "configuration_template_sha256": "b" * 64,
            "argv": ["{python}", "--baseline-timeout-run", "{baseline_run_path}"],
        },
    ]
    template: dict[str, object] = {
        "schema_version": "candidate-fast-launch-templates.v1",
        "campaign_id": "fast",
        "plan_sha256": sha256_json(plan),
        "max_parallel_runs": 4,
        "jobs_per_run": 4,
        "tasks": launch_tasks,
        "template_commitment_sha256": "0" * 64,
    }
    template["template_commitment_sha256"] = sha256_json(
        {key: value for key, value in template.items() if key != "template_commitment_sha256"}
    )
    template_path = _write(tmp_path / "control/templates.json", template)

    launch = fast_plan.materialize_fast_launch_spec(
        workspace_root=tmp_path,
        template_path=template_path,
        head_path=head_path,
        output_path=Path("control/wave.json"),
    )
    assert launch[0]["argv"][-1] == "runs/imported.json"
    assert launch[1]["argv"][-1] == "runs/dynamic.json"
    assert all(row["argv"][0] for row in launch)

    tampered = read_json(template_path)
    tampered["tasks"][0]["argv"].append("--tampered")
    tampered_path = _write(tmp_path / "control/templates-tampered.json", tampered)
    with pytest.raises(Exception, match="commitment differs"):
        fast_plan.materialize_fast_launch_spec(
            workspace_root=tmp_path,
            template_path=tampered_path,
            head_path=head_path,
            output_path=Path("control/never.json"),
        )
