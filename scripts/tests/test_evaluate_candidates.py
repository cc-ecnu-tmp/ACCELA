import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "evaluate_candidates.py"
SPEC = importlib.util.spec_from_file_location("evaluate_candidates", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class EvaluateCandidatesTest(unittest.TestCase):
    def test_metric_and_geometric_mean(self) -> None:
        self.assertEqual(MODULE.INSTRUCTION_RE.search("instructions=42 loads=1").group(1), "42")
        self.assertAlmostEqual(MODULE._geometric_mean([2.0, 8.0]), 4.0)
        with self.assertRaises(RuntimeError):
            MODULE._geometric_mean([1.0, 0.0])

    def test_defaults_use_six_by_four(self) -> None:
        args = MODULE._parser().parse_args([])
        self.assertEqual((args.max_runs, args.jobs), (6, 4))

    def test_global_progress_reaches_total(self) -> None:
        progress = MODULE.GlobalProgress(10)
        output = io.StringIO()
        with redirect_stdout(output):
            progress.advance(4)
            progress.advance(6)
        self.assertIn("10/10 (100%)", output.getvalue())

    def test_invalid_b4_cases_are_not_scheduled(self) -> None:
        manifest = json.loads(
            (MODULE.DATA / "manifests/b4-official-performance-2025-preliminary.manifest.json")
            .read_text(encoding="utf-8")
        )
        case_ids = {case["id"] for case in manifest["cases"]}
        self.assertTrue(
            {
                "rv64gc:03_sort1",
                "rv64gc:fft0",
                "rv64gc:if-combine2",
                "rv64gc:if-combine3",
            }.isdisjoint(case_ids)
        )

    def test_candidate_compositions_use_registry_order(self) -> None:
        self.assertEqual(
            MODULE._canonical_candidates(
                [
                    "candidate.rv64-word-pressure",
                    "candidate.array-object-promotion",
                    "candidate.sysy-region-memory-forwarding",
                ]
            ),
            [
                "candidate.sysy-region-memory-forwarding",
                "candidate.array-object-promotion",
                "candidate.rv64-word-pressure",
            ],
        )

    def test_failed_profile_is_not_rankable_and_gates_are_explicit(self) -> None:
        failed = {"status": "failed", "cases": [{"status": "failed", "case_id": "x"}]}
        self.assertFalse(MODULE._summary_rankable(failed))
        metrics = {
            "rankable": True,
            "failure_reasons": [],
            "stage_geometric_means": {"B3": 1.0, "B4": 1.0, "B5": 1.0, "B6": 1.0},
            "combined_geometric_mean": 1.0,
        }
        reasons = MODULE._gate_reasons(
            metrics,
            ["B3", "B4", "B5", "B6"],
            current={"combined_geometric_mean": 0.99},
            require_full=True,
        )
        self.assertIn("B3-GM<=1.0", reasons)
        self.assertIn("combined-B3-B6-GM<=1.0", reasons)


if __name__ == "__main__":
    unittest.main()
