import json
import re
import struct
import tempfile
import unittest
from pathlib import Path

from tools.targetlab.config import validate_config
from tools.targetlab.profilec import (_category_for_metric, _expected_metrics, _normalization_for_metric,
    canonical_json, load_json, profile_from_raw)
from tools.targetlab.runner import MAILBOX_MAGIC, decode_mailbox
from tools.targetlab.schema import ValidationError, validate_profile
from tools.targetlab.target.generate_registry import main as generate_registry


class TargetLabTest(unittest.TestCase):
    def setUp(self):
        self.profile = load_json(Path("config/target/boomv3-development.json"))

    def environment(self):
        return {"backend": "linux", "rdcycle": True, "rdinstret": True,
            "timer": "rdcycle", "clock_hz": 50_000_000, "minimum_cycles": 1_000_000,
            "warmup_count": 2, "sample_count": 9, "measurement_mode": "hardware"}

    def sample(self, metric, count=9):
        normalization = _normalization_for_metric(metric)
        return {"metric": metric, "category": _category_for_metric(metric), "source": "rdcycle_x1000",
            "iterations": 1000, "normalization": normalization,
            "baseline_values": [1000] * count,
            "measured_values": [1000 + 1000 * normalization] * count,
            "values": [1000] * count}

    def test_checked_in_profile_is_strict_and_canonical(self):
        self.assertIs(validate_profile(self.profile), self.profile)
        self.assertEqual(json.loads(canonical_json(self.profile)), self.profile)

    def test_unknown_profile_key_is_rejected(self):
        self.profile["unexpected"] = True
        with self.assertRaisesRegex(ValidationError, "unknown keys"):
            validate_profile(self.profile)

    def test_json_loader_rejects_duplicate_keys_and_non_finite_numbers(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid.json"
            path.write_text('{"value":1,"value":2}', encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "duplicate JSON key"):
                load_json(path)
            path.write_text('{"value":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "non-finite JSON"):
                load_json(path)

    def test_unstable_measurement_is_rejected(self):
        self.profile["operations"]["integer_alu"]["latency"]["mad"] = 0.02
        with self.assertRaisesRegex(ValidationError, "unstable"):
            validate_profile(self.profile)

    def test_mailbox_decoder_checks_identity_and_layout(self):
        name = b"operations.integer_alu.latency"
        category = b"arithmetic"
        source = b"rdcycle_x1000"
        samples = [1000] * 9
        baseline = [1000] * 9
        measured = [2000] * 9
        entry = struct.pack("<48s32s24sQQQ9Q9Q9Q", name, category, source, 9, 1000, 1,
            *baseline, *measured, *samples)
        payload = struct.pack("<QIIQQIIQQIIIIQQQ", MAILBOX_MAGIC, 1, 1, 440, 1, 3, 0,
            50_000_000, 1_000_000, 2, 9, 1, 0, (1 << 64) - 1, 0, 0) + entry
        with tempfile.TemporaryDirectory() as temporary:
            source_path = Path(temporary) / "mailbox.bin"
            output_path = Path(temporary) / "samples.jsonl"
            source_path.write_bytes(payload)
            decode_mailbox(source_path, output_path)
            lines = output_path.read_text(encoding="utf-8").splitlines()
            environment = json.loads(lines[0])
            decoded = json.loads(lines[1])
            self.assertTrue(environment["rdcycle"])
            self.assertTrue(environment["rdinstret"])
            self.assertEqual(decoded["metric"], name.decode())
            self.assertEqual(decoded["baseline_values"], baseline)
            self.assertEqual(decoded["measured_values"], measured)
            self.assertEqual(decoded["values"], samples)

    def test_mailbox_decoder_rejects_inconsistent_length(self):
        payload = struct.pack("<QIIQQIIQQIIIIQQQ", MAILBOX_MAGIC, 1, 1, 97, 0, 1, 0,
            50_000_000, 1_000_000, 2, 9, 1, 0, (1 << 64) - 1, 0, 0) + b"x"
        with tempfile.TemporaryDirectory() as temporary:
            source_path = Path(temporary) / "mailbox.bin"
            output_path = Path(temporary) / "samples.jsonl"
            source_path.write_bytes(payload)
            with self.assertRaisesRegex(RuntimeError, "length"):
                decode_mailbox(source_path, output_path)

    def test_configuration_is_strict(self):
        config = {"backend": "linux", "cc": "cc", "objcopy": "objcopy", "nm": "nm",
            "build_dir": "build/targetlab", "clock_hz": 50_000_000,
            "minimum_cycles": 1_000_000, "timeout_seconds": 3600,
            "measurement_mode": "hardware", "execute": "runner"}
        self.assertIs(validate_config(config), config)
        config["fallback"] = True
        with self.assertRaisesRegex(ValidationError, "unknown keys"):
            validate_config(config)

    def test_managed_qemu_debug_server_configuration_is_strict(self):
        config = {"backend": "baremetal", "cc": "cc", "objcopy": "objcopy", "nm": "nm",
            "build_dir": "build/targetlab", "clock_hz": 50_000_000,
            "minimum_cycles": 1_000_000, "timeout_seconds": 3600,
            "measurement_mode": "qemu_proxy", "gdb": "gdb", "gdb_remote": "localhost:3333",
            "startup": "start.S", "linker": "link.ld", "debug_server": {
                "kind": "qemu", "mode": "managed", "executable": "qemu-system-riscv64",
                "machine": "virt", "memory": "512M"}}
        self.assertIs(validate_config(config), config)
        config["debug_server"]["openocd_config"] = "forbidden.cfg"
        with self.assertRaisesRegex(ValidationError, "fields do not match"):
            validate_config(config)

    def test_calibration_requires_every_metric_and_nine_samples(self):
        raw = {"schema_version": 1, "environment": self.environment(), "samples": []}
        with self.assertRaisesRegex(ValidationError, "misses .* required metrics"):
            profile_from_raw(self.profile, raw)
        raw["samples"].append(self.sample("operations.integer_alu.latency", 1))
        with self.assertRaisesRegex(ValidationError, "exactly 9"):
            profile_from_raw(self.profile, raw)

    def test_complete_archive_calibrates_and_mirrors_pairing(self):
        raw = {"schema_version": 1, "environment": self.environment(), "samples": []}
        for metric in sorted(_expected_metrics(self.profile)):
            raw["samples"].append(self.sample(metric))
        calibrated = profile_from_raw(self.profile, raw)
        self.assertTrue(calibrated["profile"]["calibrated"])
        self.assertEqual(calibrated["profile"]["evidence_level"], "target_hardware")
        self.assertEqual(calibrated["measurement_environment"], raw["environment"])
        self.assertEqual(calibrated["pairing"]["load"]["store"],
            calibrated["pairing"]["store"]["load"])

    def test_qemu_measurement_cannot_inherit_hardware_evidence(self):
        raw = {"schema_version": 1, "environment": self.environment(), "samples": []}
        raw["environment"]["measurement_mode"] = "qemu_proxy"
        for metric in sorted(_expected_metrics(self.profile)):
            raw["samples"].append(self.sample(metric))
        calibrated = profile_from_raw(self.profile, raw)
        self.assertEqual(calibrated["profile"]["evidence_level"], "qemu_proxy")
        calibrated["profile"]["evidence_level"] = "target_hardware"
        with self.assertRaisesRegex(ValidationError, "conflicts with measurement mode"):
            validate_profile(calibrated)

    def test_raw_evidence_rejects_tampering_and_normalizes_throughput_per_operation(self):
        metric = "operations.integer_alu.throughput"
        sample = self.sample(metric)
        self.assertEqual(sample["normalization"], 256)
        raw = {"schema_version": 1, "environment": self.environment(), "samples": [sample]}
        sample["values"][3] += 1
        with self.assertRaisesRegex(ValidationError, "normalization is inconsistent"):
            profile_from_raw(self.profile, raw)

    def test_registry_covers_every_required_metric_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            registry = Path(temporary) / "registry.c"
            generate_registry(registry)
            metrics = re.findall(r'\{"([^"]+)", "[^"]+",',
                registry.read_text(encoding="utf-8"))
            self.assertEqual(len(metrics), len(set(metrics)))
            self.assertEqual(set(metrics), _expected_metrics(self.profile))


if __name__ == "__main__":
    unittest.main()
