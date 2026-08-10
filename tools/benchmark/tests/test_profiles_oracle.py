from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import tools.benchmark.campaign as campaign_module
from tools.benchmark.campaign import (
    build_campaign_plan,
    finalize_campaign_plan,
    next_campaign_tasks,
    update_campaign_status,
)
from tools.benchmark.errors import ValidationError
from tools.benchmark.inventory import inventory_cleanroom_manifest
from tools.benchmark.oracle import build_oracle_plan
from tools.benchmark.profiles import generate_ablation_profiles
from tools.benchmark.schema import load_and_validate, validate_document
from tools.benchmark.util import atomic_write_json, sha256_file, sha256_json


def test_ablate_profiles_generates_canonical_family_pair_matrix(tmp_path: Path) -> None:
    registry = {
        "schema_version": "pass-registry.v2",
        "passes": [
            {
                "id": "ir.alpha",
                "logical_family_id": "ir.family-a",
                "display_name": "Alpha",
                "stage": "ir_function",
                "full_pipeline_occurrences": 2,
                "lifecycle": "production",
                "decision_observable": True,
                "candidate_anchor": None,
                "legality_obligation_ids": [],
            },
            {
                "id": "ir.required",
                "logical_family_id": "ir.mandatory",
                "display_name": "Required",
                "stage": "ir_function",
                "full_pipeline_occurrences": 1,
                "lifecycle": "required",
                "decision_observable": False,
                "candidate_anchor": None,
                "legality_obligation_ids": [],
            },
            {
                "id": "backend.beta",
                "logical_family_id": "backend.family-b",
                "display_name": "Beta",
                "stage": "backend_function",
                "full_pipeline_occurrences": 3,
                "lifecycle": "production",
                "decision_observable": False,
                "candidate_anchor": None,
                "legality_obligation_ids": [],
            },
        ],
    }
    registry_path = tmp_path / "registry.json"
    atomic_write_json(registry_path, registry)
    output = tmp_path / "profiles-output"
    matrix = generate_ablation_profiles(
        registry_path=registry_path,
        output_directory=output,
        top_pairs=[("ir.family-a", "backend.family-b")],
    )
    assert matrix["schema_version"] == "ablation-matrix.v1"
    assert matrix["unschedulable_families"] == ["ir.mandatory"]
    assert {item["kind"] for item in matrix["profiles"]} == {
        "full",
        "mandatory",
        "family_ablation",
        "pair_ablation",
    }
    family_profile = next(item for item in matrix["profiles"] if item["profile_id"] == "without.ir.family-a")
    profile_bytes = (output / family_profile["path"]).read_bytes()
    assert profile_bytes == b'{"schema_version":2,"base":"FULL","disable":[{"pass":"ir.alpha"}],"enable_candidates":[]}\n'
    assert sha256_file(output / family_profile["path"]) == family_profile["profile_sha256"]
    first_matrix = (output / "matrix.json").read_bytes()
    second = generate_ablation_profiles(
        registry_path=registry_path,
        output_directory=output,
        top_pairs=[("ir.family-a", "backend.family-b")],
    )
    assert second == matrix
    assert (output / "matrix.json").read_bytes() == first_matrix
    load_and_validate(output / "matrix.json")


def test_oracle_plan_expands_verified_clean_room_pairs_without_paths(tmp_path: Path) -> None:
    suite = tmp_path / "corpus"
    pair = suite / "oracles" / "family" / "variant"
    pair.mkdir(parents=True)
    (pair / "baseline.sy").write_bytes(b"baseline")
    (pair / "optimized.sy").write_bytes(b"optimized")
    (pair / "medium.in").write_bytes(b"input")
    (pair / "medium.out").write_bytes(b"0\n")
    corpus_manifest = suite / "manifest.json"
    corpus_manifest.write_text(json.dumps({
        "schema_version": 1,
        "provenance_policy": {"license": "MIT"},
        "benchmarks": [],
        "structural_variants": [],
        "oracle_families": [{
            "family": "family",
            "variants": [{
                "variant": "variant",
                "baseline": "oracles/family/variant/baseline.sy",
                "optimized": "oracles/family/variant/optimized.sy",
                "datasets": [{
                    "tier": "medium",
                    "input": "oracles/family/variant/medium.in",
                    "output": "oracles/family/variant/medium.out",
                }],
            }],
        }],
    }), encoding="utf-8")
    manifest = inventory_cleanroom_manifest(
        corpus_manifest,
        suite_id="oracle-suite",
        target="rv64gc",
        data_role="oracle",
        origin_source="clean-room-generator",
        tiers=["medium"],
    )
    manifest_path = tmp_path / "oracle-manifest.json"
    atomic_write_json(manifest_path, manifest)
    plan = build_oracle_plan(
        manifest_path=manifest_path,
        suite_root=suite,
        pipeline_profile_id="full",
        pipeline_profile_sha256="b" * 64,
        baseline_run_id="campaign-new-oracle-baseline",
        optimized_run_id="campaign-new-oracle-optimized",
    )
    assert plan["schema_version"] == "oracle-plan.v1"
    assert len(plan["pairs"]) == 1
    assert plan["pipeline_profile"]["profile_id"] == "full"
    assert plan["pairs"][0]["baseline"]["case_id"].endswith(":baseline")
    assert plan["pairs"][0]["optimized"]["case_id"].endswith(":optimized")
    payload = json.dumps(plan)
    assert str(tmp_path) not in payload


def test_campaign_plan_status_and_next_are_budgeted_and_resumable(tmp_path: Path, monkeypatch) -> None:
    run_schema_source = (
        Path(campaign_module.__file__).resolve().parent / "schemas" / "run-record.v1.json"
    )
    run_schema_path = tmp_path / campaign_module._RUN_RECORD_SCHEMA_RELATIVE_PATH
    run_schema_path.parent.mkdir(parents=True)
    run_schema_bytes = run_schema_source.read_bytes()
    run_schema_path.write_bytes(run_schema_bytes)
    registry = {
        "schema_version": "pass-registry.v2",
        "passes": [
            {
                "id": f"ir.pass{index}", "logical_family_id": f"ir.family{index}",
                "display_name": f"Family {index}", "stage": "ir_function",
                "full_pipeline_occurrences": 1, "lifecycle": "production",
                "decision_observable": True,
                "candidate_anchor": None, "legality_obligation_ids": [],
            }
            for index in range(8)
        ],
    }
    registry_path = tmp_path / "registry.json"
    atomic_write_json(registry_path, registry)
    matrix_dir = tmp_path / "matrix"
    generate_ablation_profiles(
        registry_path=registry_path, output_directory=matrix_dir,
    )
    repository_root = Path(__file__).resolve().parents[3]
    manifest_root = repository_root / "docs/optimization/data/manifests"
    manifest_names = {
        "B1": "b1-official-functional-2026.manifest.json",
        "B2": "b2-family-smoke.manifest.json",
        "B3": "b3-official-performance-2026.manifest.json",
        "B4": "b4-official-performance-2025-preliminary.manifest.json",
        "B5": "b5-structural-variants.manifest.json",
        "B6": "b6-mature-benchmarks.manifest.json",
        "oracle": "oracle-cleanroom.manifest.json",
    }
    suite_paths = {}
    for role, name in manifest_names.items():
        path = tmp_path / f"{role}.json"
        shutil.copyfile(manifest_root / name, path)
        suite_paths[role] = path
    oracle_manifest = load_and_validate(suite_paths["oracle"])
    oracle_cases = oracle_manifest["cases"][:2]
    matrix = load_and_validate(matrix_dir / "matrix.json")
    full_profile = next(item for item in matrix["profiles"] if item["profile_id"] == "full")
    oracle_plan = validate_document({
        "schema_version": "oracle-plan.v1",
        "evidence_class": "cleanroom",
        "manifest_data_role": "oracle",
        "suite_id": oracle_manifest["suite_id"],
        "manifest_sha256": sha256_json(oracle_manifest),
        "pipeline_profile": {
            "profile_id": "full", "profile_sha256": full_profile["profile_sha256"],
        },
        "baseline_run_id": "campaign-oracle-baseline",
        "optimized_run_id": "campaign-oracle-optimized",
        "pairs": [{
            "pair_id": "campaign-oracle-pair",
            "family": oracle_cases[0]["family"],
            "target": "rv64gc",
            "input_sha256": None if oracle_cases[0]["input"] is None else oracle_cases[0]["input"]["sha256"],
            "expected_output_sha256": oracle_cases[0]["expected_output"]["sha256"],
            "baseline": {
                "case_id": oracle_cases[0]["id"],
                "source_group": oracle_cases[0]["source_group"],
                "source_sha256": oracle_cases[0]["source"]["sha256"],
            },
            "optimized": {
                "case_id": oracle_cases[1]["id"],
                "source_group": oracle_cases[1]["source_group"],
                "source_sha256": oracle_cases[1]["source"]["sha256"],
            },
        }],
    })
    oracle_plan_path = tmp_path / "oracle-plan.json"
    atomic_write_json(oracle_plan_path, oracle_plan)
    protocol_paths = {}
    protocol_documents = {}
    for offset, mode in enumerate(("standard_proxy", "cache_hotblock"), 1):
        protocol = validate_document({
            "schema_version": "measurement-protocol.v1",
            "protocol_id": f"campaign-{mode}",
            "measurement_mode": mode,
            "target": "rv64gc", "abi": "lp64d", "code_model": "medany",
            "input_transport": {
                "kind": "fw_cfg_dma",
                "item_name": "opt/accela/sysy-input",
                "exact_bytes": True,
                "eof": "size_delimited",
                "max_input_size_bytes": 4_294_967_295,
                "guest_buffer_size_bytes": 4_096,
                "guest_buffer_section": ".sysy_input_transport",
                "transport_section_size_bytes": 4_112,
            },
            "sources": {
                "profile_plugin_sha256": "1" * 64,
                "cache_plugin_sha256": "2" * 64,
                "hotblocks_plugin_sha256": "3" * 64,
                "runtime_filter_sha256": "4" * 64,
                "runtime_sha256": "5" * 64,
                "crt_sha256": "6" * 64,
                "linker_script_sha256": "7" * 64,
            },
            "plugin_binaries": {
                "profile_sha256": "8" * 64,
                "cache_sha256": "9" * 64,
                "hotblocks_sha256": "a" * 64,
            },
            "qemu": {
                "binary_sha256": "b" * 64, "version": "11.0.3",
                "machine": "virt", "cpu_model": "rv64", "accelerator": "tcg",
                "memory": "512M", "plugin_log_flags": ["-d", "plugin", "-D", "{metric_file}"],
                "runner_command_sha256": f"{offset:x}" * 64,
                "runner_executable_sha256": f"{offset + 2:x}" * 64,
                "runner_adapter": "wsl", "wsl_distribution_sha256": None,
            },
            "cache_model": {
                "size_bytes": 32768, "ways": 8, "line_bytes": 64,
                "replacement": "lru", "initial_state": "cold_per_timing_region",
            },
        })
        protocol_path = tmp_path / f"{mode}.json"
        atomic_write_json(protocol_path, protocol)
        protocol_paths[mode] = protocol_path
        protocol_documents[mode] = protocol
    toolchain_path = tmp_path / "toolchain.json"
    compile_driver = tmp_path / "scripts" / "reference-compile.sh"
    compile_driver.parent.mkdir()
    compile_driver.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    source_adapter = tmp_path / "tools" / "benchmark" / "reference_source.py"
    source_adapter.parent.mkdir(parents=True, exist_ok=True)
    source_adapter.write_text("# frozen adapter\n", encoding="utf-8")
    builtin_header = tmp_path / "tools" / "qemu" / "sysy-builtins.h"
    builtin_header.parent.mkdir(parents=True)
    builtin_header.write_text("/* frozen header */\n", encoding="utf-8")
    toolchain_path.write_text(json.dumps({
        "schema": "accela-toolchain-snapshot.v1",
        "target": {"isa": "rv64gc", "abi": "lp64d", "code_model": "medany"},
        "reference_frontends": {
            "compile_driver_path": "scripts/reference-compile.sh",
            "compile_driver_sha256": sha256_file(compile_driver),
            "source_adapter_path": "tools/benchmark/reference_source.py",
            "source_adapter_sha256": sha256_file(source_adapter),
            "builtin_header_path": "tools/qemu/sysy-builtins.h",
            "builtin_header_sha256": sha256_file(builtin_header),
            "frontend_language": "c++17",
            "launcher_contract": campaign_module.REFERENCE_LAUNCHER_CONTRACT,
            "local_image_id": "sha256:" + "1" * 64,
            "gcc": {
                "version": "13.3.0", "optimization": "-O2",
                "executable": campaign_module._REFERENCE_BASELINES["gcc_13_3_o2"]["executable"],
                "package": campaign_module._REFERENCE_BASELINES["gcc_13_3_o2"]["package"],
                "cxx_package": campaign_module._REFERENCE_BASELINES["gcc_13_3_o2"]["cxx_package"],
                "compiler_argv": campaign_module._REFERENCE_FRONTEND_ARGV["gcc"],
                "compiler_argv_sha256": sha256_json(
                    {"argv": campaign_module._REFERENCE_FRONTEND_ARGV["gcc"]}
                ),
            },
            "clang": {
                "version": "18.1.3", "optimization": "-O3",
                "executable": campaign_module._REFERENCE_BASELINES["clang_18_o3"]["executable"],
                "package": campaign_module._REFERENCE_BASELINES["clang_18_o3"]["package"],
                "compiler_argv": campaign_module._REFERENCE_FRONTEND_ARGV["clang"],
                "compiler_argv_sha256": sha256_json(
                    {"argv": campaign_module._REFERENCE_FRONTEND_ARGV["clang"]}
                ),
            },
            "common_semantics": campaign_module._REFERENCE_COMMON_SEMANTICS,
        },
        "proxy_execution": {
            "qemu_system_riscv64": "11.0.3", "riscv_bare_metal_linker": "15.2.0",
            "python": "3.14.6", "glib": "2.88.3", "jdk": "21.0.11",
            "measurement_protocols": {
                mode: {
                    "measurement_mode": mode,
                    "protocol_id": protocol_documents[mode]["protocol_id"],
                    "protocol_sha256": sha256_json(protocol_documents[mode]),
                    "protocol_path": protocol_paths[mode].name,
                }
                for mode in ("standard_proxy", "cache_hotblock")
            },
        },
    }, sort_keys=True), encoding="utf-8")
    plan = build_campaign_plan(
        matrix_path=matrix_dir / "matrix.json", suite_paths=suite_paths,
        oracle_plan_path=oracle_plan_path,
        measurement_protocol_path=protocol_paths["standard_proxy"],
        hotblock_measurement_protocol_path=protocol_paths["cache_hotblock"],
        reference_toolchain_path=toolchain_path,
        workspace_root=tmp_path,
        campaign_id="finals-72h", max_workers=4,
    )
    assert plan["run_record_schema_sha256"] == sha256_file(run_schema_path)
    with pytest.raises(
        ValidationError,
        match="run-record schema binding differs from the active benchmark schema",
    ):
        validate_document({**plan, "run_record_schema_sha256": "f" * 64})
    run_schema_path.write_bytes(b"{}\n")
    with pytest.raises(
        ValidationError,
        match="workspace run-record schema differs from the active benchmark schema",
    ):
        build_campaign_plan(
            matrix_path=matrix_dir / "matrix.json",
            suite_paths=suite_paths,
            oracle_plan_path=oracle_plan_path,
            measurement_protocol_path=protocol_paths["standard_proxy"],
            hotblock_measurement_protocol_path=protocol_paths["cache_hotblock"],
            reference_toolchain_path=toolchain_path,
            workspace_root=tmp_path,
            campaign_id="finals-72h",
            max_workers=4,
        )
    run_schema_path.write_bytes(run_schema_bytes)
    toolchain_document = json.loads(toolchain_path.read_text(encoding="utf-8"))
    drifted_toolchain = json.loads(json.dumps(toolchain_document))
    drifted_toolchain["proxy_execution"]["measurement_protocols"][
        "cache_hotblock"
    ]["protocol_sha256"] = "f" * 64
    atomic_write_json(toolchain_path, drifted_toolchain)
    with pytest.raises(
        ValidationError,
        match="reference toolchain protocol binding differs from supplied protocol",
    ):
        build_campaign_plan(
            matrix_path=matrix_dir / "matrix.json",
            suite_paths=suite_paths,
            oracle_plan_path=oracle_plan_path,
            measurement_protocol_path=protocol_paths["standard_proxy"],
            hotblock_measurement_protocol_path=protocol_paths["cache_hotblock"],
            reference_toolchain_path=toolchain_path,
            workspace_root=tmp_path,
            campaign_id="finals-72h",
            max_workers=4,
        )
    atomic_write_json(toolchain_path, toolchain_document)
    launcher_drift = json.loads(json.dumps(toolchain_document))
    launcher_drift["reference_frontends"]["launcher_contract"][
        "docker_fallback_policy"
    ] = "any_failure"
    atomic_write_json(toolchain_path, launcher_drift)
    with pytest.raises(
        ValidationError,
        match="reference toolchain launcher contract has drifted",
    ):
        build_campaign_plan(
            matrix_path=matrix_dir / "matrix.json",
            suite_paths=suite_paths,
            oracle_plan_path=oracle_plan_path,
            measurement_protocol_path=protocol_paths["standard_proxy"],
            hotblock_measurement_protocol_path=protocol_paths["cache_hotblock"],
            reference_toolchain_path=toolchain_path,
            workspace_root=tmp_path,
            campaign_id="finals-72h",
            max_workers=4,
        )
    atomic_write_json(toolchain_path, toolchain_document)
    assert sum(phase["budget_seconds"] for phase in plan["phases"]) == 72 * 3600
    assert [phase["budget_seconds"] for phase in plan["phases"]] == [12 * 3600, 24 * 3600, 24 * 3600, 12 * 3600]
    assert {suite["data_role"] for suite in plan["suites"]} == {
        "B1", "B2", "B3", "B4", "B5", "B6", "oracle"
    }
    assert len([task for task in plan["tasks"] if task["kind"] == "pair_ablation"]) == 0
    assert plan["final_pair_families"] == []
    assert any(task["measurement_mode"] == "cache_hotblock" for task in plan["tasks"])
    assert {
        task["compiler_baseline"] for task in plan["tasks"]
        if task["phase_id"] == "baseline_validation" and task["suite_role"] == "B3"
    } == {"accela_full", "accela_mandatory", "gcc_13_3_o2", "clang_18_o3"}
    assert all(
        task["phase_id"] == "final_validation"
        for task in plan["tasks"] if task["suite_role"] in {"B4", "B5", "B6", "oracle"}
    )
    oracle_tasks = [task for task in plan["tasks"] if task["oracle_leg"] is not None]
    assert {task["oracle_leg"] for task in oracle_tasks} == {"baseline", "optimized"}
    assert {task["run_id"] for task in oracle_tasks} == {
        oracle_plan["baseline_run_id"], oracle_plan["optimized_run_id"]
    }
    plan_path = tmp_path / "campaign-plan.json"
    atomic_write_json(plan_path, plan)
    status = update_campaign_status(
        plan_path=plan_path, run_paths={},
        started_at="2026-08-09T00:00:00Z", as_of="2026-08-09T00:00:01Z",
    )
    assert status["state"] == "pending"
    assert status["tasks"][0]["missing_reason"] == "not_scheduled"
    assert status["tasks"][0]["run_id"] == plan["tasks"][0]["run_id"]
    assert status["deadline"] == "2026-08-12T00:00:00.000Z"
    assert status["elapsed_wall_clock_seconds"] == 1
    next_tasks = next_campaign_tasks(plan, status)
    assert len(next_tasks) == 4
    assert all(item["suite_role"] in {"B1", "B2", "B3", "B4", "B5", "B6", "oracle"} for item in next_tasks)
    assert all(item["phase_deadline"] == "2026-08-09T12:00:00.000Z" for item in next_tasks)

    task_for = {
        (task["phase_id"], task["profile_id"]): task for task in plan["tasks"]
    }
    runs = {
        task["task_id"]: {"run_id": task["run_id"]}
        for task in plan["tasks"]
        if task["phase_id"] in {"baseline_validation", "singleton_b2", "promotion_b3"}
    }
    smoke_variants = []
    confirmation_variants = []
    for index in range(8):
        profile_id = f"without.ir.family{index}"
        smoke_repetitions = [1.0]
        correctness_failures = 0
        if index == 0:
            smoke_repetitions = [1.006]
        elif index == 1:
            smoke_repetitions = [1.10]
        elif index == 2:
            smoke_repetitions = [0.96]
        elif index == 3:
            correctness_failures = 1
        smoke_variants.append({
            "profile_id": profile_id,
            "run_id": task_for[("singleton_b2", profile_id)]["run_id"],
            "case_geometric_mean_contribution": smoke_repetitions[0],
            "per_cases": [{"contribution_ratio": value} for value in smoke_repetitions],
            "correctness_failures": correctness_failures,
        })
        confirmation_variants.append({
            "profile_id": profile_id,
            "run_id": task_for[("promotion_b3", profile_id)]["run_id"],
            "case_geometric_mean_contribution": 2.0 - index * 0.1,
            "eligible_for_ranking": True,
            "ineligibility_reason": None,
        })
    smoke_study = {
        "schema_version": "ablation-study.v1", "matrix_sha256": plan["matrix_sha256"],
        "study_id": "smoke", "generated_at": "2026-08-09T10:00:00Z",
        "baseline_run_id": task_for[("baseline_validation", "full")]["run_id"],
        "variants": smoke_variants,
    }
    # The phase/profile index collapses B2/B3 FULL to the later entry; use the
    # explicit suite task to bind each study baseline exactly.
    smoke_study["baseline_run_id"] = next(
        task["run_id"] for task in plan["tasks"]
        if task["suite_role"] == "B2" and task["profile_id"] == "full"
    )
    confirmation_study = {
        "schema_version": "ablation-study.v1", "matrix_sha256": plan["matrix_sha256"],
        "study_id": "confirmation", "generated_at": "2026-08-11T10:00:00Z",
        "baseline_run_id": next(
            task["run_id"] for task in plan["tasks"]
            if task["phase_id"] == "baseline_validation" and task["suite_role"] == "B3" and task["profile_id"] == "full"
        ),
        "variants": confirmation_variants,
    }
    with monkeypatch.context() as scoped:
        scoped.setattr(
            campaign_module,
            "load_and_validate",
            lambda path: smoke_study if Path(path).name == "smoke.json" else confirmation_study,
        )
        decisions = campaign_module._study_decisions(
            plan=plan, runs=runs,
            study_paths={"singleton_b2": Path("smoke.json"), "promotion_b3": Path("confirmation.json")},
        )
    assert decisions["minimum_top8_satisfied"] is True
    assert len(decisions["promoted_profile_ids"]) == 8
    assert "geometric_mean_threshold" in decisions["smoke"][0]["reasons"]
    assert "single_case_threshold" in decisions["smoke"][1]["reasons"]
    assert "regression_investigation" in decisions["smoke"][2]["reasons"]
    assert "correctness_investigation" in decisions["smoke"][3]["reasons"]
    assert decisions["final_profile_ids"] == [f"without.ir.family{index}" for index in range(5)]
    assert decisions["final_pair_coverage_complete"] is False

    promoted_status = json.loads(json.dumps(status))
    promoted_status["promotion_decisions"] = decisions
    status_path = tmp_path / "promotion-status.json"
    atomic_write_json(status_path, promoted_status)
    extended_dir = tmp_path / "extended-matrix"
    generate_ablation_profiles(
        registry_path=registry_path,
        output_directory=extended_dir,
        top_families=[f"ir.family{index}" for index in range(5)],
    )
    finalized = finalize_campaign_plan(
        plan_path=plan_path,
        status_path=status_path,
        matrix_path=extended_dir / "matrix.json",
    )
    assert finalized["parent_plan_sha256"] is not None
    assert finalized["promotion_status_sha256"] is not None
    assert (
        finalized["run_record_schema_sha256"]
        == plan["run_record_schema_sha256"]
    )
    assert finalized["final_pair_families"] == [f"ir.family{index}" for index in range(5)]
    assert len([task for task in finalized["tasks"] if task["kind"] == "pair_ablation"]) == 10
