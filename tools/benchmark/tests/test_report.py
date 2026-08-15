import unittest
import json
import tempfile
from pathlib import Path

from tools.benchmark.report import BenchmarkError, analyze, validate_manifest


def run(ratio):
    return {"baseline_seconds": ratio, "candidate_seconds": 1.0,
        "baseline_compile_seconds": 1.0, "candidate_compile_seconds": 1.1,
        "baseline_peak_bytes": 100, "candidate_peak_bytes": 110,
        "baseline_code_bytes": 1000, "candidate_code_bytes": 1010,
        "cold_start": True, "cache_reused": False}


class BenchmarkReportTest(unittest.TestCase):
    def document(self, evidence="boom_hardware"):
        return {"schema_version": 1, "comparison": "r1_full", "evidence_level": evidence,
            "target": "rv64gc", "abi": "lp64d", "runtime": "field-runtime",
            "cases": [{"id": "a", "excluded_reason": None, "runs": [run(1.1)] * 5},
                {"id": "b", "excluded_reason": None, "runs": [run(1.2)] * 5}]}

    def test_formal_gate_uses_paired_geomean_and_bootstrap(self):
        result = analyze(self.document(), bootstrap_samples=1000)
        self.assertTrue(result["formal_evidence"])
        self.assertTrue(result["gate_passed"])
        self.assertAlmostEqual(result["gm"], (1.1 * 1.2) ** 0.5)

    def test_qemu_cannot_pass_formal_gate(self):
        result = analyze(self.document("qemu_proxy"), bootstrap_samples=100)
        self.assertFalse(result["formal_evidence"])
        self.assertFalse(result["gate_passed"])

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


if __name__ == "__main__":
    unittest.main()
