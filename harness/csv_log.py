"""Thread-safe, durable CSV event logging for harness timelines."""

from __future__ import annotations

import csv
import os
import threading
import time
from datetime import datetime
from pathlib import Path


CSV_FIELDS = (
    "row_type",
    "wall_timestamp",
    "monotonic_timestamp",
    "device_time",
    "sequence_number",
    "trigger_code",
    "error_code",
    "error_string",
    "ready",
    "latency_ms",
    "outcome",
    "marker",
    "detail",
)


class CsvEventLog:
    """Write complete timeline rows that survive process termination."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        empty = not self.path.exists() or self.path.stat().st_size == 0
        self._stream = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._stream, fieldnames=CSV_FIELDS)
        self._closed = False
        if empty:
            self._writer.writeheader()

    def _write(self, row_type, **fields):
        with self._lock:
            row = dict.fromkeys(CSV_FIELDS, "")
            row.update(
                row_type=row_type,
                wall_timestamp=datetime.now()
                .astimezone()
                .isoformat(timespec="microseconds"),
                monotonic_timestamp=time.monotonic(),
            )
            row.update(fields)
            self._writer.writerow(row)
            self._stream.flush()
            os.fsync(self._stream.fileno())

    def write_trigger(
        self,
        *,
        device_time,
        sequence_number,
        trigger_code,
        error_code,
        error_string,
        ready,
        latency_ms,
        outcome,
        detail="",
    ):
        self._write(
            "trigger",
            device_time=device_time,
            sequence_number=sequence_number,
            trigger_code=trigger_code,
            error_code=error_code,
            error_string=error_string,
            ready=ready,
            latency_ms=latency_ms,
            outcome=outcome,
            detail=detail,
        )

    def write_marker(self, marker, detail="", **fields):
        self._write("marker", marker=marker, detail=detail, **fields)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
