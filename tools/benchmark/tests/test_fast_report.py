from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tools.benchmark.cli import build_parser
from tools.benchmark.errors import ValidationError
from tools.benchmark.fast_report import (
    _ARTIFACT_FILENAMES,
    _candidate_ineligibility_reasons,
    _publish_create_only,
    _report_payloads,
    _report_markdown,
    _svg_single_candidate_ci,
    _svg_document,
    _validate_manifest,
    fast_report_input_commitments_from_projection,
    load_and_verify_fast_report_manifest,
)
from tools.benchmark.util import canonical_json_bytes, sha256_bytes, sha256_json


def _projection() -> dict:
    def stage(name: str, speedup: float, count: int) -> dict:
        return {
            "eligible": True,
            "geometric_mean_speedup": speedup,
            "case_count": count,
            "confidence_interval_95": (
                {"low": 1.08, "high": 1.12} if name == "B3" else None
            ),
            "baseline_run_id": f"run.{name}.full",
            "candidate_run_id": f"run.{name}.candidate.safe",
        }

    stages = {
        "B2": stage("B2", 1.01, 20),
        "B3": stage("B3", 1.10, 60),
        "B4": stage("B4", 1.11, 59),
        "B5": stage("B5", 1.12, 60),
        "B6": stage("B6", 1.13, 88),
    }
    candidate = {
        "candidate_id": "candidate.safe",
        "promoted": True,
        "eligible_for_final": True,
        "ineligibility_reasons": [],
        "combined_case_count": 267,
        "combined_geometric_mean_speedup": 1.115,
        "b3_geometric_mean_speedup": 1.10,
        "combined_static_text_bytes_candidate": 1234.0,
        "combined_static_text_ratio": 1.01,
        "rank": 1,
        "risk": "low",
        "ranking_run_ids": [
            {
                "stage": name,
                "baseline_run_id": stages[name]["baseline_run_id"],
                "candidate_run_id": stages[name]["candidate_run_id"],
            }
            for name in ("B3", "B4", "B5", "B6")
        ],
        "stages": stages,
    }
    other_b3 = {
        "eligible": True,
        "geometric_mean_speedup": 0.95,
        "case_count": 60,
        "confidence_interval_95": {"low": 0.93, "high": 0.97},
        "baseline_run_id": "run.B3.full",
        "candidate_run_id": "run.B3.candidate.other",
    }
    other = {
        "candidate_id": "candidate.other",
        "promoted": False,
        "eligible_for_final": False,
        "ineligibility_reasons": ["not_promoted_by_B3"],
        "combined_case_count": 60,
        "combined_geometric_mean_speedup": None,
        "b3_geometric_mean_speedup": 0.95,
        "combined_static_text_bytes_candidate": None,
        "combined_static_text_ratio": None,
        "rank": None,
        "risk": "high",
        "ranking_run_ids": [
            {
                "stage": "B3",
                "baseline_run_id": "run.B3.full",
                "candidate_run_id": "run.B3.candidate.other",
            }
        ],
        "stages": {
            "B2": None,
            "B3": other_b3,
            "B4": None,
            "B5": None,
            "B6": None,
        },
    }
    return {
        "campaign_id": "fast",
        "evidence_level": "qemu_proxy",
        "bootstrap_sha256": "a" * 64,
        "plan_sha256": "b" * 64,
        "index_sha256": "c" * 64,
        "status_sha256": "d" * 64,
        "audits": {
            key: {"canonical_sha256": (str(index + 1) * 64)}
            for index, key in enumerate(("bootstrap", "B2", "B3", "final"))
        },
        "studies": {
            "B2": {"canonical_sha256": "5" * 64},
            "B3": {"canonical_sha256": "6" * 64},
            "B4": {"canonical_sha256": "7" * 64},
            "B5": {"canonical_sha256": "8" * 64},
            "B6": {"canonical_sha256": "9" * 64},
        },
        "diagnostic_study": {"canonical_sha256": "a" * 64},
        "diagnostic_top3": ["candidate.safe", "candidate.other"],
        "diagnostic_pair_count": 1,
        "diagnostic_pairs": [
            {
                "candidate_ids": ["candidate.other", "candidate.safe"],
                "geometric_mean_speedup": 1.04,
                "expected_multiplicative_speedup": 1.045,
                "delta_ln_geometric_mean": -0.004796,
                "interaction_factor": 0.9952,
                "eligible": True,
                "reason": None,
                "run_id": "run.diagnostic.pair",
            }
        ],
        "cache_hotblock": [
            {
                "label": "FULL",
                "run_id": "run.diagnostic.cache.full",
                "mean_l1d_misses_per_1000_dynamic_loads": 12.0,
                "mean_hottest_block_dynamic_instruction_share_percent": 31.0,
                "sample_count": 2,
                "reason": None,
            },
            {
                "label": "candidate.safe",
                "run_id": "run.diagnostic.cache.safe",
                "mean_l1d_misses_per_1000_dynamic_loads": 10.0,
                "mean_hottest_block_dynamic_instruction_share_percent": 35.0,
                "sample_count": 2,
                "reason": None,
            },
        ],
        "oracle_capture": {
            "pair_count": 99,
            "capture_sha256": "e" * 64,
            "baseline_run_id": "oracle.baseline",
            "optimized_run_id": "oracle.optimized",
            "rows": [
                {
                    "candidate_id": "candidate.safe",
                    "oracle_upper_bound": 1.25,
                    "b3_measured_speedup": 1.10,
                    "capture_rate": 0.88,
                    "eligible_structures": 2,
                    "qualifying_structures": 1,
                    "qualification_status": "qualified",
                    "reason": None,
                },
                {
                    "candidate_id": "candidate.other",
                    "oracle_upper_bound": 1.20,
                    "b3_measured_speedup": 0.95,
                    "capture_rate": 0.7916666666666666,
                    "eligible_structures": 1,
                    "qualifying_structures": 1,
                    "qualification_status": "qualified",
                    "reason": None,
                },
            ],
        },
        "reference_gaps": [
            {
                "reference": "GCC",
                "status": "complete",
                "gap_ratio": 0.8,
                "case_count": 60,
                "reason": None,
                "full_run_id": "run.B3.full",
                "reference_run_id": "run.B3.gcc",
            }
        ],
        "candidates": [candidate, other],
        "ranking": [
            {
                "rank": 1,
                "candidate_id": "candidate.safe",
                "combined_geometric_mean_speedup": 1.115,
                "b3_geometric_mean_speedup": 1.10,
                "combined_static_text_bytes_full_plus_candidate": 1234.0,
                "combined_static_text_ratio": 1.01,
                "stable_id_tiebreak": "candidate.safe",
            }
        ],
    }


def test_rendered_report_is_semantic_and_svg_is_parseable() -> None:
    projection = _projection()
    report = _report_markdown(projection).decode("utf-8")
    assert "B3-B6 (267 cases)" in report
    assert "B2 remains a mandatory correctness and coverage eligibility gate" in report
    assert "candidate.safe" in report
    assert "qemu_proxy" in report
    assert "D:\\" not in report and "/tmp/" not in report

    svg = _svg_document(
        "Eligible candidate ranking",
        [("candidate.safe", 1.115)],
        x_label="B3-B6 geometric mean speedup",
    )
    root = ET.fromstring(svg)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert b"candidate.safe" in svg


def test_report_payloads_use_seven_distinct_semantic_charts() -> None:
    payloads = _report_payloads(_projection())
    charts = {
        key: ET.fromstring(payload)
        for key, payload in payloads.items()
        if key.endswith("chart") or key == "pair_heatmap"
    }
    assert len(charts) == 7
    assert all(root.tag == "{http://www.w3.org/2000/svg}svg" for root in charts.values())

    single = charts["single_candidate_chart"]
    assert single.attrib["data-chart-kind"] == "confidence-interval"
    assert len(single.findall(".//*[@data-role='ci95']")) == 2
    assert b"10000" in payloads["single_candidate_chart"]

    heatmap = payloads["pair_heatmap"]
    assert b"candidate.safe" in heatmap and b"candidate.other" in heatmap
    assert heatmap.count(b"-0.5") >= 2
    assert b"run.diagnostic.pair" in heatmap

    cache = payloads["cache_hotblock_chart"]
    assert b"L1D misses / 1000 dynamic loads" in cache
    assert b"run.diagnostic.cache.safe" in cache

    pareto = payloads["pareto_chart"]
    assert b"<circle" in pareto and b"risk=low" in pareto
    assert b"1234 B" in pareto

    oracle = payloads["oracle_capture_chart"]
    assert b"oracle.baseline" in oracle and b"oracle.optimized" in oracle
    assert b"pair_count&quot;:99" in oracle
    assert b"candidate.safe / Oracle" in oracle

    suite = payloads["suite_chart"]
    assert b"candidate.safe / B3" in suite and b"candidate.safe / B6" in suite
    assert b"candidate.safe / B2" not in suite
    ranking = payloads["ranking_chart"]
    assert b"267-case" in ranking and b"run.B6.candidate.safe" in ranking


def test_single_candidate_chart_rejects_invalid_interval() -> None:
    with pytest.raises(ValidationError, match="confidence interval"):
        _svg_single_candidate_ci(
            [("candidate.safe", 1.1, 1.2, 1.3)], evidence={"run_ids": []}
        )


def test_b2_failure_excludes_otherwise_winning_candidate_without_entering_gm() -> None:
    stages = {
        stage: {"eligible": True, "reason": None}
        for stage in ("B2", "B3", "B4", "B5", "B6")
    }
    stages["B2"] = {"eligible": False, "reason": "correctness_failure"}
    assert _candidate_ineligibility_reasons(
        is_promoted=True, stage_results=stages
    ) == ["B2:correctness_failure"]


def test_report_manifest_is_committed_and_verifies_create_only_files(
    tmp_path: Path,
) -> None:
    output = tmp_path / "report"
    output.mkdir()
    chart_rows = [("candidate.safe", 1.1)]
    payloads = {
        "report": b"# report\n",
        "single_candidate_chart": _svg_document(
            "single", chart_rows, x_label="speedup", evidence={"run_ids": ["single-run"]}
        ),
        "suite_chart": _svg_document(
            "suites", [("candidate.safe / B3", 1.1)], x_label="speedup",
            evidence={"run_ids": ["suite-run"]},
        ),
        "ranking_chart": _svg_document(
            "ranking", chart_rows, x_label="speedup", evidence={"run_ids": ["ranking-run"]}
        ),
        "pair_heatmap": _svg_document(
            "pairs", chart_rows, x_label="speedup", evidence={"run_ids": ["pair-run"]}
        ),
        "oracle_capture_chart": _svg_document(
            "oracle", chart_rows, x_label="capture", evidence={"run_ids": ["oracle-run"]}
        ),
        "cache_hotblock_chart": _svg_document(
            "cache", chart_rows, x_label="speedup", evidence={"run_ids": ["cache-run"]}
        ),
        "pareto_chart": _svg_document(
            "pareto", chart_rows, x_label="speedup", evidence={"run_ids": ["pareto-run"]}
        ),
    }
    filenames = {
        "report": "report.md",
        "single_candidate_chart": "single_candidate_chart.svg",
        "suite_chart": "suite_chart.svg",
        "ranking_chart": "ranking_chart.svg",
        "pair_heatmap": "pair_heatmap.svg",
        "oracle_capture_chart": "oracle_capture_chart.svg",
        "cache_hotblock_chart": "cache_hotblock_chart.svg",
        "pareto_chart": "pareto_chart.svg",
    }
    files = {}
    for artifact_id, payload in payloads.items():
        path = output / filenames[artifact_id]
        _publish_create_only(path, payload, label=artifact_id)
        _publish_create_only(path, payload, label=artifact_id)
        files[artifact_id] = {
            "path": path.relative_to(tmp_path).as_posix(),
            "physical_sha256": sha256_bytes(payload),
            "size_bytes": len(payload),
            "media_type": "text/markdown; charset=utf-8" if artifact_id == "report" else "image/svg+xml",
        }
        assert not Path(files[artifact_id]["path"]).is_absolute()
        assert b"D:\\" not in payload and b"/tmp/" not in payload
        if artifact_id != "report":
            assert ET.fromstring(payload).tag == "{http://www.w3.org/2000/svg}svg"
            assert payload == _svg_document(
                {
                    "single_candidate_chart": "single",
                    "suite_chart": "suites",
                    "ranking_chart": "ranking",
                    "pair_heatmap": "pairs",
                    "oracle_capture_chart": "oracle",
                    "cache_hotblock_chart": "cache",
                    "pareto_chart": "pareto",
                }[artifact_id],
                [("candidate.safe / B3", 1.1)] if artifact_id == "suite_chart" else chart_rows,
                x_label="capture" if artifact_id == "oracle_capture_chart" else "speedup",
                evidence={
                    "run_ids": [
                        {
                            "single_candidate_chart": "single-run",
                            "suite_chart": "suite-run",
                            "ranking_chart": "ranking-run",
                            "pair_heatmap": "pair-run",
                            "oracle_capture_chart": "oracle-run",
                            "cache_hotblock_chart": "cache-run",
                            "pareto_chart": "pareto-run",
                        }[artifact_id]
                    ]
                },
            )
    commitments = {
        "bootstrap_sha256": "a" * 64,
        "plan_sha256": "b" * 64,
        "index_sha256": "c" * 64,
        "status_sha256": "d" * 64,
        "diagnostic_study_sha256": "e" * 64,
        "audit_sha256s": {key: "f" * 64 for key in ("bootstrap", "B2", "B3", "final")},
        "study_sha256s": {key: None for key in ("B2", "B3", "B4", "B5", "B6")},
    }
    manifest = {
        "schema_version": "candidate-fast-report-manifest.v1",
        "campaign_id": "fast",
        "evidence_level": "qemu_proxy",
        "input_commitments": commitments,
        "ranking": [],
        "ranking_sha256": sha256_json([]),
        "files": files,
        "manifest_commitment_sha256": "0" * 64,
    }
    manifest["manifest_commitment_sha256"] = sha256_json(
        {key: value for key, value in manifest.items() if key != "manifest_commitment_sha256"}
    )
    _validate_manifest(manifest)
    manifest_path = output / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    assert load_and_verify_fast_report_manifest(
        workspace_root=tmp_path,
        manifest_path=manifest_path,
        expected_input_commitments=commitments,
    ) == manifest

    with pytest.raises(ValidationError, match="different bytes"):
        _publish_create_only(output / "report.md", b"changed\n", label="report")


def test_manifest_projection_check_rejects_self_consistent_forged_chart(
    tmp_path: Path,
) -> None:
    projection = _projection()
    payloads = _report_payloads(projection)
    output = tmp_path / "report"
    output.mkdir()
    files = {}
    for artifact_id, filename in _ARTIFACT_FILENAMES.items():
        payload = payloads[artifact_id]
        (output / filename).write_bytes(payload)
        files[artifact_id] = {
            "path": f"report/{filename}",
            "physical_sha256": sha256_bytes(payload),
            "size_bytes": len(payload),
            "media_type": (
                "text/markdown; charset=utf-8"
                if artifact_id == "report"
                else "image/svg+xml"
            ),
        }
    manifest = {
        "schema_version": "candidate-fast-report-manifest.v1",
        "campaign_id": projection["campaign_id"],
        "evidence_level": "qemu_proxy",
        "input_commitments": fast_report_input_commitments_from_projection(
            projection
        ),
        "ranking": projection["ranking"],
        "ranking_sha256": sha256_json(projection["ranking"]),
        "files": files,
        "manifest_commitment_sha256": "0" * 64,
    }

    def publish_manifest() -> None:
        manifest["manifest_commitment_sha256"] = sha256_json(
            {
                key: value
                for key, value in manifest.items()
                if key != "manifest_commitment_sha256"
            }
        )
        (output / "manifest.json").write_bytes(
            canonical_json_bytes(manifest) + b"\n"
        )

    publish_manifest()
    assert load_and_verify_fast_report_manifest(
        workspace_root=tmp_path,
        manifest_path=output / "manifest.json",
        expected_projection=projection,
    ) == manifest

    forged = payloads["ranking_chart"].replace(
        b"267-case combined ranking", b"forged ranking chart      "
    )
    (output / _ARTIFACT_FILENAMES["ranking_chart"]).write_bytes(forged)
    manifest["files"]["ranking_chart"].update(
        physical_sha256=sha256_bytes(forged), size_bytes=len(forged)
    )
    publish_manifest()
    with pytest.raises(ValidationError, match="differs from its manifest"):
        load_and_verify_fast_report_manifest(
            workspace_root=tmp_path,
            manifest_path=output / "manifest.json",
            expected_projection=projection,
        )


def test_fast_report_cli_is_registered() -> None:
    args = build_parser().parse_args(
        [
            "candidates", "fast-report", "--workspace-root", ".",
            "--bootstrap", "bootstrap.json", "--plan", "plan.json",
            "--index", "index.json", "--status", "status.json",
            "--audit", "bootstrap=a.json", "--study", "B2=s.json",
            "--diagnostic-study", "diagnostic.json",
            "--output-directory", "report",
        ]
    )
    assert args.candidates_command == "fast-report"
