import json
import struct
import tempfile
import unittest
from pathlib import Path

from tools.targetlab.profilec import canonical_json, load_json
from tools.targetlab.runner import MAILBOX_MAGIC, decode_mailbox
from tools.targetlab.schema import ValidationError, validate_profile


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
        payload = struct.pack("<QIIQ", MAILBOX_MAGIC, 1, 1, 1) + entry
        with tempfile.TemporaryDirectory() as temporary:
            source_path = Path(temporary) / "mailbox.bin"
            output_path = Path(temporary) / "samples.jsonl"
            source_path.write_bytes(payload)
            decode_mailbox(source_path, output_path)
            decoded = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(decoded["metric"], name.decode())
            self.assertEqual(decoded["values"], samples)


if __name__ == "__main__":
    unittest.main()
