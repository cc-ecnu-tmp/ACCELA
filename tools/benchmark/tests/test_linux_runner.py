import tempfile
from pathlib import Path
import unittest

from tools.benchmark.run_linux_paired import _case_ids, _judge_output, _render


class LinuxPairedRunnerTest(unittest.TestCase):
    def test_case_list_accepts_crlf_and_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cases.txt"
            path.write_bytes(b"\xef\xbb\xbfcase_a\r\ncase-b\r\n")
            self.assertEqual(["case_a", "case-b"], _case_ids(path))
            path.write_text("case_a\ncase_a\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "duplicate"):
                _case_ids(path)

    def test_judge_output_matches_sysy_stdout_plus_exit_contract(self):
        self.assertEqual(b"text\n3\n", _judge_output(b"text", 3))
        self.assertEqual(b"line\n0\n", _judge_output(b"line\r\n", 0))
        self.assertEqual(b"7\n", _judge_output(b"", 7))

    def test_float_rendering_round_trips_for_ratio_validation(self):
        value = 0.002887592123456789
        self.assertEqual(value, float(_render(value)))


if __name__ == "__main__":
    unittest.main()
