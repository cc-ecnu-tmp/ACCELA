import unittest
import json
import tempfile
from pathlib import Path

from tools.benchmark.report import (BenchmarkError, analyze, import_linux_tsv, import_qemu_tsv, render,
                                    validate_manifest)


def run(ratio):
    return {"baseline_runtime": ratio, "candidate_runtime": 1.0,
        "baseline_compile_seconds": 1.0, "candidate_compile_seconds": 1.1,
        "baseline_peak_bytes": 100, "candidate_peak_bytes": 110,
        "baseline_code_bytes": 1000, "candidate_code_bytes": 1010,
        "cold_start": True, "cache_reused": False}


class BenchmarkReportTest(unittest.TestCase):
    def document(self, evidence="boom_hardware"):
        return {"schema_version": 2, "comparison": "r1_full", "runtime_metric": "seconds",
            "evidence_level": evidence,
            "target": "rv64gc", "abi": "lp64d", "runtime": "field-runtime",
            "cases": [{"id": "a", "excluded_reason": None, "runs": [run(1.1)] * 5},
                {"id": "b", "excluded_reason": None, "runs": [run(1.2)] * 5}]}

    def test_formal_gate_uses_paired_geomean_and_bootstrap(self):
        result = analyze(self.document(), bootstrap_samples=1000)
        self.assertTrue(result["formal_evidence"])
        self.assertTrue(result["gate_passed"])
        self.assertAlmostEqual(result["gm"], (1.1 * 1.2) ** 0.5)

    def test_qemu_cannot_pass_formal_gate(self):
        document = self.document("qemu_proxy")
        document["runtime_metric"] = "instructions"
        result = analyze(document, bootstrap_samples=100)
        self.assertFalse(result["formal_evidence"])
        self.assertFalse(result["gate_passed"])

    def test_runtime_metric_must_match_evidence_and_report_includes_resources(self):
        document = self.document()
        document["runtime_metric"] = "instructions"
        with self.assertRaisesRegex(BenchmarkError, "conflicts"):
            analyze(document)
        document["runtime_metric"] = "seconds"
        report = render(document, analyze(document, bootstrap_samples=10))
        self.assertIn("Compile seconds median", report)
        self.assertIn("Peak RSS bytes max", report)
        self.assertIn("Code `.text` bytes median", report)
        self.assertIn("| a | 1.100000 |", report)

    def test_r2_r1_no_regression_uses_non_strict_lower_bound(self):
        document = self.document()
        document["comparison"] = "r2_r1"
        for case in document["cases"]:
            case["runs"] = [run(1.0)] * 5
        result = analyze(document, bootstrap_samples=100)
        self.assertTrue(result["gate_passed"])

    def test_requires_five_cold_uncached_pairs(self):
        document = self.document()
        document["cases"][0]["runs"] = document["cases"][0]["runs"][:4]
        with self.assertRaisesRegex(BenchmarkError, "at least five"):
            analyze(document)
        document = self.document()
        document["cases"][0]["runs"][0] = dict(document["cases"][0]["runs"][0],
            cache_reused=True)
        with self.assertRaisesRegex(BenchmarkError, "cold start"):
            analyze(document)

    def test_manifest_preflight_rejects_path_escape_and_missing_required_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "case.sy").write_text("int main(){return 0;}", encoding="utf-8")
            (root / "case.out").write_text("0\n", encoding="utf-8")
            manifest = {"schema_version": 1, "target": "rv64gc", "abi": "lp64d",
                "runtime": "field", "max_static_bytes": 1024, "cases": [{"id": "case",
                    "source": "case.sy", "input": None, "input_required": True,
                    "expected": "case.out", "timeout_seconds": 1,
                    "expected_static_bytes": 0, "excluded_reason": None}]}
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(BenchmarkError, "requires an input"):
                validate_manifest(path)
            manifest["cases"][0]["input_required"] = False
            manifest["cases"][0]["source"] = "../escape.sy"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(BenchmarkError, "escapes"):
                validate_manifest(path)

    def test_imports_measured_qemu_tsv_without_inventing_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "results.tsv"
            rows = [f"case\t{index}\t100\t100\t1.000000000\t0.1\t0.2\t1000\t2000\t3000\t3000"
                for index in range(1, 6)]
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            document = import_qemu_tsv(path, "r2_r1", "rv64gc", "lp64d", "qemu-tcg")
            self.assertEqual("qemu_proxy", document["evidence_level"])
            self.assertEqual("instructions", document["runtime_metric"])
            self.assertEqual(5, len(document["cases"][0]["runs"]))
            self.assertFalse(analyze(document, bootstrap_samples=10)["gate_passed"])

    def test_imports_linux_hardware_seconds_without_claiming_boom(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "results.tsv"
            rows = [f"case\t{index}\t0.2\t0.1\t2.000000000\t0.1\t0.2\t1000\t2000\t3000\t2900"
                for index in range(1, 6)]
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            document = import_linux_tsv(path, "r2_r1", "rv64gc", "lp64d", "visionfive2")
            self.assertEqual("target_hardware", document["evidence_level"])
            self.assertEqual("seconds", document["runtime_metric"])
            result = analyze(document, bootstrap_samples=10)
            self.assertEqual(2.0, result["gm"])
            self.assertFalse(result["formal_evidence"])
            self.assertFalse(result["gate_passed"])


if __name__ == "__main__":
    unittest.main()
