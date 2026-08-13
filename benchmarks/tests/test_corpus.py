# SPDX-License-Identifier: MIT

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("accela_benchmark_generator", ROOT / "generate.py")
assert SPEC is not None and SPEC.loader is not None
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)

VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "accela_benchmark_validator", ROOT / "validate.py"
)
assert VALIDATOR_SPEC is not None and VALIDATOR_SPEC.loader is not None
validator = importlib.util.module_from_spec(VALIDATOR_SPEC)
sys.modules[VALIDATOR_SPEC.name] = validator
VALIDATOR_SPEC.loader.exec_module(validator)


EXPECTED_POLYBENCH = {
    "pb_2mm_i32", "pb_3mm_i32", "pb_atax_i32", "pb_bicg_i32",
    "pb_gemm_i32", "pb_gemver_i32", "pb_gesummv_i32", "pb_mvt_i32",
    "pb_dynprog_i32", "pb_durbin_f32", "pb_jacobi1d_i32",
    "pb_jacobi2d_i32", "pb_seidel2d_i32", "pb_fdtd2d_i32",
}
EXPECTED_EMBENCH = {
    "eb_aha_mont64", "eb_crc32", "eb_huffbench", "eb_matmult_int",
    "eb_nettle_aes", "eb_nettle_sha256", "eb_statemate", "eb_wikisort",
}
EXPECTED_ORACLES = {
    "closed_form", "dp_storage", "prefix_scan", "linear_transition",
    "fusion", "structured_kernel", "memoization", "bitset",
    "finite_state", "recursion_worklist", "boom_ilp",
}
EXPECTED_STRUCTURAL_TAXONOMIES = {
    "01_mm", "03_sort", "conv2d", "crc", "crypto", "fft", "h-1", "h-10",
    "h-4", "h-5", "h-8", "h-9", "huffman", "knapsack_naive",
    "many_mat_cal", "matmul", "optimization_scheduling", "shuffle", "sl",
    "transpose",
}
EXPECTED_VARIANT_KINDS = {
    "cfg_induction_equivalent", "small_deterministic",
    "large_different_deterministic",
}


class CorpusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))

    def test_primary_program_inventory(self) -> None:
        by_group: dict[str, set[str]] = {}
        for item in self.manifest["benchmarks"]:
            by_group.setdefault(item["group"], set()).add(item["id"])
            self.assertEqual(4, len(item["datasets"]))
            self.assertEqual(
                {
                    "correctness": "correctness",
                    "small": "performance",
                    "medium": "performance",
                    "large": "performance",
                },
                {dataset["tier"]: dataset["role"] for dataset in item["datasets"]},
            )
        self.assertEqual(EXPECTED_POLYBENCH, by_group["polybench-style"])
        self.assertEqual(EXPECTED_EMBENCH, by_group["embench-style"])
        expected_sources = {str((ROOT / item["source"]).resolve()) for item in self.manifest["benchmarks"]}
        actual_sources = {str(path.resolve()) for path in (ROOT / "programs").glob("*.sy")}
        self.assertEqual(expected_sources, actual_sources)

    def test_oracle_inventory(self) -> None:
        families = {item["family"]: item for item in self.manifest["oracle_families"]}
        self.assertEqual(EXPECTED_ORACLES, set(families))
        for family in families.values():
            self.assertEqual(3, len(family["variants"]))
            for variant in family["variants"]:
                self.assertEqual(3, len(variant["datasets"]))
                self.assertTrue((ROOT / variant["baseline"]).is_file())
                self.assertTrue((ROOT / variant["optimized"]).is_file())
        expected_sources = {
            str((ROOT / variant[role]).resolve())
            for family in families.values()
            for variant in family["variants"]
            for role in ("baseline", "optimized")
        }
        actual_sources = {
            str(path.resolve()) for path in (ROOT / "oracles").glob("*/*/*.sy")
        }
        self.assertEqual(expected_sources, actual_sources)

    def test_structural_variant_inventory(self) -> None:
        grouped: dict[str, set[str]] = {}
        for item in self.manifest["structural_variants"]:
            self.assertEqual("structural_variant", item["role"])
            self.assertEqual("MIT", item["spdx"])
            grouped.setdefault(item["family_taxonomy"], set()).add(item["variant_kind"])
            for key in ("source", "input", "output"):
                self.assertTrue((ROOT / item[key]).is_file())
        self.assertEqual(EXPECTED_STRUCTURAL_TAXONOMIES, set(grouped))
        for variants in grouped.values():
            self.assertEqual(EXPECTED_VARIANT_KINDS, variants)
        self.assertEqual(60, len(self.manifest["structural_variants"]))
        expected_sources = {
            str((ROOT / item["source"]).resolve())
            for item in self.manifest["structural_variants"]
        }
        actual_sources = {
            str(path.resolve())
            for path in (ROOT / "structural_variants").glob("*/*.sy")
        }
        self.assertEqual(expected_sources, actual_sources)

    def test_generator_is_clean(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "generate.py"), "--check"],
            cwd=ROOT.parent, text=True, capture_output=True, timeout=60, check=False,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)

    def test_i32_reference_semantics(self) -> None:
        self.assertEqual(-2147483648, generator.add32(2147483647, 1))
        self.assertEqual(2147483647, generator.sub32(-2147483648, 1))
        self.assertEqual(1410065408, generator.mul32(100000, 100000))
        self.assertEqual(-2, generator.div32(-7, 3))
        self.assertEqual(-1, generator.rem32(-7, 3))
        self.assertEqual(-2147483648, generator.div32(-2147483648, -1))

    def test_binary32_reference_semantics(self) -> None:
        self.assertEqual(16777216.0, generator.f32(16777217.0))
        self.assertEqual(0.5, generator.fdiv(1.0, 2.0))
        bits = lambda value: generator.struct.pack("<f", value)
        expected = generator.f32(generator.f32(0.1) + generator.f32(0.2))
        self.assertEqual(bits(expected), bits(generator.fadd(0.1, 0.2)))

    def test_float_reference_implementations_agree(self) -> None:
        benchmark = next(
            item for item in self.manifest["benchmarks"] if item["id"] == "pb_durbin_f32"
        )
        self.assertEqual(
            ["python-struct-binary32", "ctypes-c-float-binary32"],
            benchmark["reference_validation"],
        )
        for dataset in benchmark["datasets"]:
            struct_values = generator.durbin_struct_reference(dataset["n"], dataset["seed"])
            ctypes_values = generator.durbin_ctypes_reference(dataset["n"], dataset["seed"])
            self.assertEqual(
                [generator.binary32_bits(value) for value in struct_values],
                [generator.binary32_bits(value) for value in ctypes_values],
            )

    def test_interpreter_transcript_is_split_without_normalization(self) -> None:
        self.assertEqual(
            (b"42\n", 0),
            validator.split_interpreter_transcript(b"42\n\n0\n", b"\n"),
        )
        self.assertEqual(
            (b"42\n", 255),
            validator.split_interpreter_transcript(b"42\n\r\n255\r\n", b"\r\n"),
        )
        expected_stdout, expected_exit = validator.split_expected_output(b"42\n0\n")
        self.assertEqual(b"42\n", expected_stdout)
        self.assertEqual(0, expected_exit)
        self.assertNotEqual(expected_stdout, b"42 \n")

    def test_sources_are_clean_room_and_path_free(self) -> None:
        source_paths = [ROOT / item["source"] for item in self.manifest["benchmarks"]]
        for family in self.manifest["oracle_families"]:
            for variant in family["variants"]:
                source_paths.extend((ROOT / variant["baseline"], ROOT / variant["optimized"]))
        for item in self.manifest.get("structural_variants", []):
            source_paths.append(ROOT / item["source"])
        for source in source_paths:
            text = source.read_text(encoding="utf-8")
            self.assertIn("SPDX-License-Identifier: MIT", text)
            self.assertIn("ACCELA clean-room original", text)
            self.assertNotIn(chr(58) + chr(92), text)
            self.assertNotIn("/home/", text)
            self.assertNotIn("/Users/", text)

    def test_manifest_seeds_are_reproducible(self) -> None:
        self.assertEqual(generator.CORPUS_SEED, self.manifest["corpus_seed"])
        self.assertEqual(generator.STRUCTURE_VARIANT_SEED, self.manifest["structure_variant_seed"])
        for item in self.manifest["benchmarks"]:
            for dataset in item["datasets"]:
                self.assertEqual(generator.stable_seed(item["id"], dataset["tier"]), dataset["seed"])
        for item in self.manifest["structural_variants"]:
            identifier = f"structural:{item['family_taxonomy']}:{item['variant_kind']}"
            self.assertEqual(
                generator.stable_structure_seed(identifier, "fixed"), item["seed"]
            )

    def test_generated_artifacts_have_no_orphans(self) -> None:
        expected: set[Path] = set()
        for item in self.manifest["benchmarks"]:
            expected.add(ROOT / item["source"])
            for dataset in item["datasets"]:
                expected.update((ROOT / dataset["input"], ROOT / dataset["output"]))
        for family in self.manifest["oracle_families"]:
            for variant in family["variants"]:
                expected.update((ROOT / variant["baseline"], ROOT / variant["optimized"]))
                for dataset in variant["datasets"]:
                    expected.update((ROOT / dataset["input"], ROOT / dataset["output"]))
        for item in self.manifest["structural_variants"]:
            expected.update(ROOT / item[key] for key in ("source", "input", "output"))
        actual = {
            path
            for path in ROOT.rglob("*")
            if path.is_file() and path.suffix in {".sy", ".in", ".out"}
        }
        self.assertEqual(expected, actual)


if __name__ == "__main__":
    unittest.main()
