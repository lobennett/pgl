import csv
import io
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from harness.csv_log import CSV_FIELDS, CsvEventLog


def test_trigger_and_marker_share_one_csv_schema(tmp_path):
    path = tmp_path / "run.csv"
    log = CsvEventLog(path)
    log.write_trigger(
        device_time=1.25,
        sequence_number=4,
        trigger_code=4,
        error_code="DPX_SUCCESS",
        error_string="Success",
        ready=True,
        latency_ms=2.5,
        outcome="sent",
        detail="",
    )
    log.write_marker("kvm_switch_requested", "operator prompted")
    log.close()

    rows = list(csv.DictReader(path.open(newline="")))
    assert rows[0]["row_type"] == "trigger"
    assert rows[0]["trigger_code"] == "4"
    assert rows[1]["row_type"] == "marker"
    assert rows[1]["marker"] == "kvm_switch_requested"
    assert rows[0].keys() == rows[1].keys() == set(CSV_FIELDS)


def test_each_completed_row_is_flushed_and_fsynced(monkeypatch, tmp_path):
    fsync_calls = []
    real_fsync = os.fsync

    def track_fsync(fd):
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", track_fsync)
    path = tmp_path / "durable.csv"
    with CsvEventLog(path) as log:
        log.write_marker("first")
        log.write_marker("second")

    rows = list(csv.DictReader(path.open(newline="")))
    assert [row["marker"] for row in rows] == ["first", "second"]
    assert len(fsync_calls) == 2


def test_row_timestamps_are_timezone_aware_and_monotonic(tmp_path):
    path = tmp_path / "timestamps.csv"
    with CsvEventLog(path) as log:
        log.write_marker("first")
        log.write_marker("second")

    rows = list(csv.DictReader(path.open(newline="")))
    wall_times = [datetime.fromisoformat(row["wall_timestamp"]) for row in rows]
    monotonic_times = [float(row["monotonic_timestamp"]) for row in rows]
    assert all(value.tzinfo is not None for value in wall_times)
    assert monotonic_times == sorted(monotonic_times)


def test_concurrent_writes_remain_complete_rows(tmp_path):
    path = tmp_path / "threaded.csv"
    with CsvEventLog(path) as log:
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(
                executor.map(
                    lambda number: log.write_marker(
                        "thread_event", detail=str(number), sequence_number=number
                    ),
                    range(40),
                )
            )

    rows = list(csv.DictReader(path.open(newline="")))
    assert len(rows) == 40
    assert {int(row["sequence_number"]) for row in rows} == set(range(40))
    assert all(row["marker"] == "thread_event" for row in rows)


def test_empty_existing_file_gets_one_header_and_context_closes_once(
    monkeypatch, tmp_path
):
    class CountingStream(io.StringIO):
        def __init__(self):
            super().__init__()
            self.close_calls = 0

        def fileno(self):
            return 42

        def close(self):
            self.close_calls += 1
            super().close()

    stream = CountingStream()
    path = tmp_path / "empty.csv"
    path.touch()
    monkeypatch.setattr(type(path), "open", lambda *args, **kwargs: stream)
    monkeypatch.setattr(os, "fsync", lambda fd: None)

    with CsvEventLog(path) as log:
        log.write_marker("opened")
    log.close()

    assert stream.close_calls == 1
