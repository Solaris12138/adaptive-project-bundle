# -*- coding: utf-8 -*-
"""Run: python -B -m unittest -v test_evidence_time.py"""
from decimal import Decimal, localcontext
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "adaptive-project" / "scripts" / "evidence_time.py"
# Allow isolated candidate verification before integration into release/tests.
if not SCRIPT.exists():
    SCRIPT = Path(__file__).resolve().with_name("evidence_time.py")
spec = importlib.util.spec_from_file_location("evidence_time", str(SCRIPT))
evidence_time = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence_time)


class EvidenceTimeTests(unittest.TestCase):
    def test_real_restart_interval(self):
        self.assertEqual(evidence_time.elapsed_seconds("2026-11-11T16:25:49.2035066+00:00",
                                                      "2026-11-11T16:28:49.2201725+00:00"),
                         Decimal("180.0166659"))

    def test_windows_seven_digits(self):
        # Captured from [DateTimeOffset]::Now.ToString('o') on the test host;
        # whole seconds cross-checked with .NET DateTimeOffset.ToUnixTimeSeconds.
        self.assertEqual(evidence_time.timestamp_epoch("2026-11-12T00:59:54.5988111+08:00"),
                         Decimal("1794416394.5988111"))
        self.assertEqual(evidence_time.timestamp_epoch("2026-11-12T09:14:05.1234567+08:00"),
                         Decimal("1794446045.1234567"))

    def test_all_fraction_lengths(self):
        for length in range(1, 10):
            fraction = "123456789"[:length]
            with self.subTest(length=length):
                self.assertEqual(evidence_time.timestamp_epoch("1970-01-01T00:00:00." + fraction + "Z"),
                                 Decimal("0." + fraction))

    def test_positive_and_negative_offsets(self):
        for text in ("1970-01-01T00:00:00Z", "1970-01-01T08:00:00+08:00",
                     "1969-12-31T18:30:00-05:30", "1970-01-01T00:00:00+00:00",
                     "1970-01-01T00:00:00-00:00"):
            with self.subTest(text=text):
                self.assertEqual(evidence_time.timestamp_epoch(text), Decimal(0))

    def test_cross_day_and_leap_day(self):
        self.assertEqual(evidence_time.elapsed_seconds("2024-02-28T23:59:59.999999999Z",
                                                      "2024-02-29T00:00:00Z"), Decimal("0.000000001"))
        self.assertEqual(evidence_time.elapsed_seconds("2024-02-29T23:59:59Z",
                                                      "2024-03-01T00:00:00Z"), Decimal(1))

    def test_pre_epoch_fraction(self):
        self.assertEqual(evidence_time.timestamp_epoch("1969-12-31T23:59:59.999999999Z"),
                         Decimal("-0.000000001"))

    def test_threshold_is_not_rounded_up(self):
        start = "2026-11-12T09:00:00.0000001+08:00"
        self.assertEqual(evidence_time.elapsed_seconds(start, "2026-11-12T09:03:00.0000000+08:00"),
                         Decimal("179.9999999"))
        self.assertEqual(evidence_time.elapsed_seconds(start, "2026-11-12T09:03:00.0000001+08:00"),
                         Decimal("180.0000000"))

    def test_independent_of_decimal_context(self):
        with localcontext() as ctx:
            ctx.prec = 2
            self.assertEqual(evidence_time.timestamp_epoch("2026-11-12T09:14:05.1234567+08:00"),
                             Decimal("1794446045.1234567"))
            self.assertEqual(evidence_time.elapsed_seconds("2026-11-12T00:00:00.0000001Z",
                                                          "2026-11-12T00:03:00Z"),
                             Decimal("179.9999999"))

    def test_invalid_timestamps_fail_closed(self):
        invalid = [None, 0, "", "NaN", "2026-11-12", "2026-11-12T00:00:00",
                   "2026-11-12 00:00:00Z", "2026-11-12t00:00:00z", "2026-11-12T00:00:00.Z",
                   "2026-11-12T00:00:00.1234567890Z", "2026-11-12T00:00:00,1Z",
                   "2026-11-12T00:00:00+24:00", "2026-11-12T00:00:00-08:60",
                   "2026-11-12T00:00:00+0800", "2026-11-12T00:00:00+08:00:00",
                   "2026-11-12T00:00:00Z\n", " 2026-11-12T00:00:00Z",
                   "2025-02-29T00:00:00Z", "2026-13-01T00:00:00Z",
                   "2026-11-12T24:00:00Z", "2026-11-12T00:60:00Z",
                   "2026-11-12T00:00:60Z", "0000-01-01T00:00:00Z"]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(evidence_time.TimestampError):
                    evidence_time.timestamp_epoch(value)

    def test_reversed_interval_even_one_nanosecond(self):
        with self.assertRaises(evidence_time.TimestampError):
            evidence_time.elapsed_seconds("2026-11-12T00:00:00.000000001Z", "2026-11-12T00:00:00Z")

    def test_interval_uses_instants_not_local_clock(self):
        self.assertEqual(evidence_time.elapsed_seconds("2026-11-12T08:00:00+08:00",
                                                      "2026-11-12T00:03:00Z"), Decimal(180))
        with self.assertRaises(evidence_time.TimestampError):
            evidence_time.elapsed_seconds("2026-11-12T00:00:00Z", "2026-11-12T01:00:00+08:00")

    def cli(self, *args, **kwargs):
        process = subprocess.run([sys.executable, "-B", str(SCRIPT)] + list(args),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 universal_newlines=True, **kwargs)
        self.assertEqual(process.stderr, "")
        value = json.loads(process.stdout)
        self.assertEqual(process.stdout, json.dumps(value, ensure_ascii=True, sort_keys=True,
                                                   separators=(",", ":")) + "\n")
        return process.returncode, value

    def test_cli_default_and_threshold(self):
        start = "2026-11-12T09:00:00.0000001+08:00"
        for end, expected, code in (("2026-11-12T09:03:00.0000000+08:00", "179.999999900", 1),
                                    ("2026-11-12T09:03:00.0000001+08:00", "180.000000000", 0)):
            with self.subTest(end=end):
                status, result = self.cli("interval", "--start", start, "--end", end)
                self.assertEqual(status, code)
                self.assertTrue(result["ok"])
                self.assertEqual(result["passed"], code == 0)
                self.assertEqual(result["elapsed_seconds"], expected)
                self.assertEqual(result["minimum_seconds"], "180")
                self.assertEqual((result["start"], result["end"]), (start, end))
                self.assertIsNone(result["error"])

    def test_cli_minimum_remains_exact(self):
        status, result = self.cli("interval", "--start", "1970-01-01T00:00:00Z",
                                  "--end", "1970-01-01T00:00:01Z", "--minimum", "1.00000000000000000000000000001")
        self.assertEqual(status, 1)
        self.assertFalse(result["passed"])

    def test_cli_invalid_minimum(self):
        for value in ("NaN", "sNaN", "Infinity", "-1", "1e2", " 180", "180\n"):
            with self.subTest(value=value):
                status, result = self.cli("interval", "--start", "1970-01-01T00:00:00Z",
                                          "--end", "1970-01-01T00:03:00Z", "--minimum", value)
                self.assertEqual(status, 2)
                self.assertFalse(result["ok"])
                self.assertFalse(result["passed"])
                self.assertIsNone(result["elapsed_seconds"])

    def test_cli_errors_are_json(self):
        for args in ([], ["interval"], ["unknown"],
                     ["interval", "--start", "NaN", "--end", "1970-01-01T00:03:00Z"],
                     ["interval", "--start", "1970-01-01T00:00:00", "--end", "1970-01-01T00:03:00Z"],
                     ["interval", "--start", "1970-01-01T00:03:00Z", "--end", "1970-01-01T00:00:00Z"]):
            with self.subTest(args=args):
                status, result = self.cli(*args)
                self.assertEqual(status, 2)
                self.assertFalse(result["passed"])
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])

    def test_no_working_directory_or_source_side_effects(self):
        with tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parent)) as folder:
            marker = Path(folder) / "state.sqlite3"
            marker.write_bytes(b"read-only evidence sentinel")
            source_before = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
            before = {str(p.relative_to(folder)): p.read_bytes() for p in Path(folder).rglob("*") if p.is_file()}
            source_listing = sorted(str(p) for p in SCRIPT.parent.rglob("*"))
            status, _ = self.cli("interval", "--start", "1970-01-01T00:00:00Z",
                                  "--end", "1970-01-01T00:03:00Z", cwd=folder)
            self.assertEqual(status, 0)
            self.assertEqual(before, {str(p.relative_to(folder)): p.read_bytes() for p in Path(folder).rglob("*") if p.is_file()})
            self.assertEqual(source_before, hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
            self.assertEqual(source_listing, sorted(str(p) for p in SCRIPT.parent.rglob("*")))


if __name__ == "__main__":
    unittest.main()
