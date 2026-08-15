import unittest

from tools.benchmark.report import BenchmarkError, analyze


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


if __name__ == "__main__":
    unittest.main()
