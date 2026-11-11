# -*- coding: utf-8 -*-
"""Exact, read-only timestamp evidence arithmetic (Python 3.8 standard library).

Accepted profile: YYYY-MM-DDTHH:MM:SS[.1-to-9-digits](Z|+HH:MM|-HH:MM).
Calendar dates and clock fields must be valid; leap seconds are not supported.
Offsets must be less than 24 hours. No whitespace or implicit local timezone is
accepted. Original timestamp strings are retained in CLI output. No runtime,
project, host, timezone database, or environment configuration is accessed.

API: timestamp_epoch(text) and elapsed_seconds(start, end) return exact Decimal
seconds. Invalid input or a reversed interval raises TimestampError (ValueError).
CLI: interval --start TEXT --end TEXT [--minimum 180]
Exit 0: meets minimum; 1: below minimum; 2: invalid input (fail closed).
JSON decimals are strings, avoiding downstream JSON float precision loss.
"""
import argparse
from datetime import datetime
from decimal import Decimal
import json
import re
import sys


_NANOSECONDS = 1000000000
_EPOCH_ORDINAL = datetime(1970, 1, 1).toordinal()
_TIMESTAMP = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T"
    r"([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})"
)
_MINIMUM = re.compile(r"[0-9]+(?:\.[0-9]+)?")


class TimestampError(ValueError):
    """Timestamp evidence cannot be used safely."""


def _timestamp_nanoseconds(text):
    if not isinstance(text, str):
        raise TimestampError("timestamp must be a string")
    match = _TIMESTAMP.fullmatch(text)
    if match is None:
        raise TimestampError("timestamp must use YYYY-MM-DDTHH:MM:SS[.1-9 digits]Z or a signed HH:MM offset")
    year, month, day, hour, minute, second = (
        int(value) for value in match.groups()[:6]
    )
    try:
        local = datetime(year, month, day, hour, minute, second)
    except ValueError as exc:
        raise TimestampError("invalid calendar date or clock time: " + str(exc))
    fraction, zone = match.groups()[6:]
    offset_seconds = 0
    if zone != "Z":
        offset_hours, offset_minutes = int(zone[1:3]), int(zone[4:6])
        if offset_hours > 23 or offset_minutes > 59:
            raise TimestampError("timezone offset must be less than 24 hours with minutes 00-59")
        offset_seconds = (offset_hours * 60 + offset_minutes) * 60
        if zone[0] == "-":
            offset_seconds = -offset_seconds
    whole_seconds = (
        (local.toordinal() - _EPOCH_ORDINAL) * 86400
        + hour * 3600 + minute * 60 + second - offset_seconds
    )
    return whole_seconds * _NANOSECONDS + int((fraction or "0").ljust(9, "0"))


def _decimal_nanoseconds(value):
    # Tuple construction is exact even under a caller's low-precision context.
    return Decimal((int(value < 0), tuple(int(digit) for digit in str(abs(value))), -9))


def timestamp_epoch(text):
    """Return exact seconds since the Unix epoch, including up to 9 decimals."""
    return _decimal_nanoseconds(_timestamp_nanoseconds(text))


def elapsed_seconds(start, end):
    """Return exact nonnegative elapsed seconds; reject reversed intervals."""
    difference = _timestamp_nanoseconds(end) - _timestamp_nanoseconds(start)
    if difference < 0:
        raise TimestampError("end timestamp precedes start timestamp")
    return _decimal_nanoseconds(difference)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise TimestampError(message)


def main(argv=None):
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    interval = commands.add_parser("interval", help="compare exact timestamp evidence")
    interval.add_argument("--start", required=True)
    interval.add_argument("--end", required=True)
    interval.add_argument("--minimum", default="180", help="nonnegative decimal seconds (default: 180)")
    result = {
        "ok": False, "passed": False, "start": None, "end": None,
        "elapsed_seconds": None, "minimum_seconds": None, "error": None,
    }
    try:
        args = parser.parse_args(argv)
        result.update(start=args.start, end=args.end, minimum_seconds=args.minimum)
        if _MINIMUM.fullmatch(args.minimum) is None:
            raise TimestampError("minimum must be finite nonnegative decimal seconds")
        minimum = Decimal(args.minimum)
        elapsed = elapsed_seconds(args.start, args.end)
        passed = elapsed >= minimum
        result.update(ok=True, passed=passed, elapsed_seconds=format(elapsed, "f"))
        code = 0 if passed else 1
    except TimestampError as exc:
        result["error"] = str(exc)
        code = 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main())
