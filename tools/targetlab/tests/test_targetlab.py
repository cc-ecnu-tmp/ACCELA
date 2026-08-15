import json
import re
import struct
import tempfile
import unittest
from pathlib import Path

from tools.targetlab.config import validate_config
from tools.targetlab.profilec import _expected_metrics, canonical_json, load_json, profile_from_raw
from tools.targetlab.runner import MAILBOX_MAGIC, decode_mailbox
from tools.targetlab.schema import ValidationError, validate_profile
from tools.targetlab.target.generate_registry import main as generate_registry


class TargetLabTest(unittest.TestCase):
    def setUp(self):
        self.profile = load_json(Path("config/target/boomv3-development.json"))

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
        samples = [1000 + index for index in range(9)]
        entry = struct.pack("<48s16s24sQ9Q", name, category, source, 9, *samples)
        payload = struct.pack("<QIIQQII", MAILBOX_MAGIC, 1, 1, 208, 1, 3, 0) + entry
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
            self.assertEqual(decoded["values"], samples)

    def test_mailbox_decoder_rejects_inconsistent_length(self):
        payload = struct.pack("<QIIQQII", MAILBOX_MAGIC, 1, 1, 41, 0, 1, 0) + b"x"
        with tempfile.TemporaryDirectory() as temporary:
            source_path = Path(temporary) / "mailbox.bin"
            output_path = Path(temporary) / "samples.jsonl"
            source_path.write_bytes(payload)
            with self.assertRaisesRegex(RuntimeError, "length"):
                decode_mailbox(source_path, output_path)

    def test_configuration_is_strict(self):
        config = {"backend": "linux", "cc": "cc", "objcopy": "objcopy", "nm": "nm",
            "build_dir": "build/targetlab", "clock_hz": 50_000_000,
            "minimum_cycles": 1_000_000, "timeout_seconds": 3600, "execute": "runner"}
        self.assertIs(validate_config(config), config)
        config["fallback"] = True
        with self.assertRaisesRegex(ValidationError, "unknown keys"):
            validate_config(config)

    def test_calibration_requires_every_metric_and_nine_samples(self):
        raw = {"schema_version": 1, "environment": {"backend": "linux",
            "rdcycle": True, "rdinstret": True, "timer": "rdcycle"}, "samples": []}
        with self.assertRaisesRegex(ValidationError, "misses .* required metrics"):
            profile_from_raw(self.profile, raw)
        raw["samples"].append({"metric": "operations.integer_alu.latency",
            "category": "arithmetic", "source": "rdcycle_x1000", "values": [1000]})
        with self.assertRaisesRegex(ValidationError, "exactly 9"):
            profile_from_raw(self.profile, raw)

    def test_complete_archive_calibrates_and_mirrors_pairing(self):
        raw = {"schema_version": 1, "environment": {"backend": "linux",
            "rdcycle": True, "rdinstret": True, "timer": "rdcycle"}, "samples": []}
        for metric in sorted(_expected_metrics(self.profile)):
            raw["samples"].append({"metric": metric, "category": "test",
                "source": "rdcycle_x1000", "values": [1000] * 9})
        calibrated = profile_from_raw(self.profile, raw)
        self.assertTrue(calibrated["profile"]["calibrated"])
        self.assertEqual(calibrated["measurement_environment"], raw["environment"])
        self.assertEqual(calibrated["pairing"]["load"]["store"],
            calibrated["pairing"]["store"]["load"])

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
