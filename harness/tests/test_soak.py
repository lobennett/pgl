import csv
import os
import signal
import subprocess
import sys
import time

import pytest

from harness.csv_log import CsvEventLog
from harness.dpx_worker import WorkerProtocolError, WorkerTimeout
from harness import soak
from harness.soak import SoakConfig, SoakRunner, build_parser, main


def successful_result(device_time=1.0):
    return {
        "ok": True,
        "ready": True,
        "error_code": "DPX_SUCCESS",
        "error_string": "Success",
        "device_time": device_time,
        "emitted": True,
        "electrically_verified": False,
        "outcome": "sent",
    }


class ScriptedWorker:
    def __init__(self, results, start_result=None):
        self.results = list(results)
        self.start_result = start_result or {
            "ok": True,
            "ready": True,
            "error_code": "DPX_SUCCESS",
            "error_string": "Success",
            "device_time": 0.0,
            "firmware_revision": 42,
            "pixel_mode": False,
            "layout": {
                "base_address": 8_000_000,
                "samples_per_condition": 4,
                "bytes_per_condition": 8,
            },
        }
        self.closed = False

    def start(self, timeout, **kwargs):
        if isinstance(self.start_result, BaseException):
            raise self.start_result
        return self.start_result

    def trigger(self, code, flush=True, timeout=None, **kwargs):
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self, timeout=1.0, **kwargs):
        self.closed = True

    def terminate(self):
        self.closed = True


def run_soak(tmp_path, config, *workers):
    worker_iter = iter(workers)
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            config,
            event_log,
            worker_factory=lambda worker_config: next(worker_iter),
        )
        runner.run()
    return runner


def csv_rows(path):
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def trigger_rows(path):
    return [row for row in csv_rows(path) if row["row_type"] == "trigger"]


def marker_names(path):
    return [
        row["marker"]
        for row in csv_rows(path)
        if row["row_type"] == "marker"
    ]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class AdvancingStopEvent:
    def __init__(self, clock):
        self.clock = clock
        self.waits = []

    def is_set(self):
        return False

    def wait(self, seconds):
        self.waits.append(seconds)
        self.clock.now += seconds
        return False


class InterruptingStopEvent(AdvancingStopEvent):
    def wait(self, seconds):
        self.waits.append(seconds)
        if seconds > 0:
            return True
        return False


def test_codes_cycle_without_zero(tmp_path):
    worker = ScriptedWorker([successful_result()] * 4)
    config = SoakConfig(interval=0, num_bits=2, max_triggers=4)
    run_soak(tmp_path, config, worker)
    rows = trigger_rows(tmp_path / "run.csv")
    assert [int(row["trigger_code"]) for row in rows] == [1, 2, 3, 1]
    assert [int(row["sequence_number"]) for row in rows] == [1, 2, 3, 4]


def test_failure_logs_and_reopens_before_continuing(tmp_path):
    first = ScriptedWorker(
        [
            {
                "ok": False,
                "ready": False,
                "error_code": "DPX_ERR_USB",
                "error_string": "gone",
                "device_time": None,
                "emitted": False,
                "electrically_verified": False,
                "outcome": "device_error",
            }
        ]
    )
    recovered = ScriptedWorker([successful_result(device_time=2.0)])
    config = SoakConfig(interval=0, reopen_interval=0, max_triggers=2)
    run_soak(tmp_path, config, first, recovered)
    rows = trigger_rows(tmp_path / "run.csv")
    assert [row["outcome"] for row in rows] == ["device_error", "sent"]
    assert marker_names(tmp_path / "run.csv").count("reopen_attempt") == 1
    assert marker_names(tmp_path / "run.csv").count("reopen_failed") == 0
    assert marker_names(tmp_path / "run.csv").count("reopen_succeeded") == 1


def test_last_allowed_failed_trigger_does_not_reopen(tmp_path):
    """Would catch opening a replacement after no trigger attempts remain."""
    failed = ScriptedWorker(
        [
            {
                "ok": False,
                "ready": False,
                "error_code": "DPX_ERR_USB",
                "error_string": "gone",
                "device_time": None,
                "emitted": False,
                "outcome": "device_error",
            }
        ]
    )
    unused_replacement = ScriptedWorker([successful_result()])
    workers = iter([failed, unused_replacement])
    factory_calls = []
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            SoakConfig(interval=0, reopen_interval=0, max_triggers=1),
            event_log,
            worker_factory=lambda worker_config: (
                factory_calls.append(worker_config) or next(workers)
            ),
        )
        runner.run()

    assert len(factory_calls) == 1
    assert not any(
        marker.startswith("reopen_")
        for marker in marker_names(tmp_path / "run.csv")
    )


@pytest.mark.parametrize(
    ("exception", "outcome"),
    [
        (WorkerTimeout("hung"), "worker_timeout"),
        (WorkerProtocolError("broken reply"), "worker_protocol_error"),
        (RuntimeError("unexpected"), "worker_exception"),
    ],
)
def test_worker_exceptions_produce_one_failure_row_then_recover(
    tmp_path, exception, outcome
):
    failed = ScriptedWorker([exception])
    recovered = ScriptedWorker([successful_result(device_time=2.0)])
    config = SoakConfig(interval=0, reopen_interval=0, max_triggers=2)
    run_soak(tmp_path, config, failed, recovered)

    rows = trigger_rows(tmp_path / "run.csv")
    assert len(rows) == 2
    assert [row["outcome"] for row in rows] == [outcome, "sent"]
    assert rows[0]["error_code"] == type(exception).__name__
    assert rows[0]["error_string"] == str(exception)


def test_failed_reopen_writes_exactly_one_failure_marker_per_attempt(tmp_path):
    failed_trigger = ScriptedWorker([WorkerTimeout("hung")])
    refused = ScriptedWorker(
        [], start_result=WorkerProtocolError("open pipe failed")
    )
    recovered = ScriptedWorker([successful_result()])
    config = SoakConfig(interval=0, reopen_interval=0, max_triggers=2)
    run_soak(tmp_path, config, failed_trigger, refused, recovered)

    markers = marker_names(tmp_path / "run.csv")
    recovery_markers = [name for name in markers if name.startswith("reopen_")]
    assert recovery_markers == [
        "reopen_attempt",
        "reopen_failed",
        "reopen_attempt",
        "reopen_succeeded",
    ]


def test_initial_open_exception_is_logged_and_recovered(tmp_path):
    failed_open = ScriptedWorker([], start_result=WorkerTimeout("open hung"))
    recovered = ScriptedWorker([successful_result()])
    config = SoakConfig(interval=0, reopen_interval=0, max_triggers=1)

    run_soak(tmp_path, config, failed_open, recovered)

    rows = csv_rows(tmp_path / "run.csv")
    open_failed = next(row for row in rows if row["marker"] == "open_failed")
    assert open_failed["error_code"] == "WorkerTimeout"
    assert open_failed["error_string"] == "open hung"
    assert [row["marker"] for row in rows if row["marker"].startswith("reopen_")] == [
        "reopen_attempt",
        "reopen_succeeded",
    ]
    assert [row["outcome"] for row in rows if row["row_type"] == "trigger"] == [
        "sent"
    ]


def test_stop_after_first_trigger_closes_worker_and_logs_close(tmp_path):
    worker = ScriptedWorker([successful_result()])
    config = SoakConfig(interval=0, max_triggers=10)
    worker_iter = iter([worker])
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            config,
            event_log,
            worker_factory=lambda worker_config: next(worker_iter),
        )
        original_trigger = worker.trigger

        def trigger_and_stop(code, flush=True, timeout=None, **kwargs):
            result = original_trigger(
                code,
                flush=flush,
                timeout=timeout,
                **kwargs,
            )
            runner.stop_event.set()
            return result

        worker.trigger = trigger_and_stop
        runner.run()

    assert worker.closed is True
    assert len(trigger_rows(tmp_path / "run.csv")) == 1
    assert marker_names(tmp_path / "run.csv")[-1] == "worker_closed"


def test_close_exception_logs_failure_without_graceful_marker(tmp_path):
    """Would catch labeling an exceptional device close as worker_closed."""
    class CloseFailingWorker(ScriptedWorker):
        def close(self, timeout=1.0, **kwargs):
            raise RuntimeError("DPxClose exploded")

    worker = CloseFailingWorker([])
    run_soak(tmp_path, SoakConfig(max_triggers=0), worker)

    markers = marker_names(tmp_path / "run.csv")
    assert "worker_close_failed" in markers
    assert "worker_closed" not in markers
    assert worker.closed is True


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(2)])
def test_base_interrupts_propagate_after_worker_cleanup(tmp_path, interrupt):
    worker = ScriptedWorker([interrupt])
    config = SoakConfig(interval=0, max_triggers=1)
    expected_type = type(interrupt)

    with pytest.raises(expected_type):
        run_soak(tmp_path, config, worker)

    assert worker.closed is True


def test_trigger_waits_use_absolute_deadlines(monkeypatch, tmp_path):
    class FakeClock:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            return self.now

    class AdvancingStopEvent:
        def __init__(self, clock):
            self.clock = clock
            self.waits = []

        def is_set(self):
            return False

        def wait(self, seconds):
            self.waits.append(seconds)
            self.clock.now += seconds
            return False

    clock = FakeClock()
    stop_event = AdvancingStopEvent(clock)
    worker = ScriptedWorker([successful_result()] * 3)
    original_trigger = worker.trigger

    def slow_trigger(code, flush=True, timeout=None, **kwargs):
        result = original_trigger(
            code,
            flush=flush,
            timeout=timeout,
            **kwargs,
        )
        clock.now += 0.25
        return result

    worker.trigger = slow_trigger
    monkeypatch.setattr(soak.time, "monotonic", clock.monotonic)
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            SoakConfig(interval=1.0, max_triggers=3),
            event_log,
            worker_factory=lambda worker_config: worker,
        )
        runner.stop_event = stop_event
        runner.run()

    assert stop_event.waits == [0.0, 0.75, 0.75]


def test_successful_recovery_rebases_trigger_cadence_without_catch_up(
    monkeypatch, tmp_path
):
    """Would catch a long recovery draining missed trigger deadlines in a burst."""
    clock = FakeClock()
    stop_event = AdvancingStopEvent(clock)
    trigger_times = []

    class TimedWorker(ScriptedWorker):
        def trigger(self, code, flush=True, timeout=None, **kwargs):
            trigger_times.append(clock.now)
            return super().trigger(
                code,
                flush=flush,
                timeout=timeout,
                **kwargs,
            )

    failed = TimedWorker(
        [
            {
                "ok": False,
                "ready": False,
                "error_code": "DPX_ERR_USB",
                "error_string": "gone",
                "device_time": None,
                "emitted": False,
                "outcome": "device_error",
            }
        ]
    )
    recovered = TimedWorker([successful_result(), successful_result()])
    workers = iter([failed, recovered])
    monkeypatch.setattr(soak.time, "monotonic", clock.monotonic)

    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            SoakConfig(
                interval=2,
                reopen_interval=7,
                heartbeat_interval=100,
                max_triggers=3,
            ),
            event_log,
            worker_factory=lambda worker_config: next(workers),
        )
        runner.stop_event = stop_event
        runner.run()

    assert trigger_times == [0, 7, 9]


def test_successful_blocked_trigger_skips_expired_cadence_deadlines(
    monkeypatch, tmp_path
):
    """Would catch a long successful call causing an immediate catch-up burst."""
    clock = FakeClock()
    stop_event = AdvancingStopEvent(clock)
    trigger_times = []

    class SlowFirstTriggerWorker(ScriptedWorker):
        def trigger(self, code, flush=True, timeout=None, **kwargs):
            trigger_times.append(clock.now)
            result = super().trigger(
                code,
                flush=flush,
                timeout=timeout,
                **kwargs,
            )
            if len(trigger_times) == 1:
                clock.now = 3.4
            return result

    worker = SlowFirstTriggerWorker(
        [successful_result(), successful_result()]
    )
    monkeypatch.setattr(soak.time, "monotonic", clock.monotonic)
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            SoakConfig(
                interval=1,
                heartbeat_interval=100,
                max_triggers=2,
            ),
            event_log,
            worker_factory=lambda worker_config: worker,
        )
        runner.stop_event = stop_event
        runner.run()

    assert trigger_times == [0, 4]


def test_duration_caps_long_normal_interval_at_absolute_deadline(
    monkeypatch, tmp_path
):
    clock = FakeClock()
    stop_event = AdvancingStopEvent(clock)
    worker = ScriptedWorker([successful_result()])
    monkeypatch.setattr(soak.time, "monotonic", clock.monotonic)
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            SoakConfig(
                interval=100,
                heartbeat_interval=1000,
                duration=1,
            ),
            event_log,
            worker_factory=lambda worker_config: worker,
        )
        runner.stop_event = stop_event
        runner.run()

    assert clock.now == 1
    assert stop_event.waits == [0.0, 1.0]
    assert len(trigger_rows(tmp_path / "run.csv")) == 1


def test_duration_caps_long_reopen_interval_without_reopen_attempt(
    monkeypatch, tmp_path
):
    clock = FakeClock()
    stop_event = AdvancingStopEvent(clock)
    worker = ScriptedWorker([WorkerTimeout("trigger hung")])
    monkeypatch.setattr(soak.time, "monotonic", clock.monotonic)
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            SoakConfig(
                interval=100,
                reopen_interval=100,
                heartbeat_interval=1000,
                duration=1,
            ),
            event_log,
            worker_factory=lambda worker_config: worker,
        )
        runner.stop_event = stop_event
        runner.run()

    assert clock.now == 1
    assert stop_event.waits == [0.0, 1.0]
    assert len(trigger_rows(tmp_path / "run.csv")) == 1
    assert [
        name
        for name in marker_names(tmp_path / "run.csv")
        if name.startswith("reopen_")
    ] == []


def test_heartbeats_fire_at_each_deadline_during_long_normal_wait(
    monkeypatch, tmp_path, capsys
):
    clock = FakeClock()
    stop_event = AdvancingStopEvent(clock)
    worker = ScriptedWorker([successful_result()])
    monkeypatch.setattr(soak.time, "monotonic", clock.monotonic)
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            SoakConfig(interval=10, heartbeat_interval=1, duration=3.5),
            event_log,
            worker_factory=lambda worker_config: worker,
        )
        runner.stop_event = stop_event
        runner.run()

    heartbeats = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("heartbeat ")
    ]
    assert heartbeats == [
        "heartbeat elapsed=1.000s attempted_triggers=1",
        "heartbeat elapsed=2.000s attempted_triggers=1",
        "heartbeat elapsed=3.000s attempted_triggers=1",
    ]
    assert clock.now == 3.5
    assert len(trigger_rows(tmp_path / "run.csv")) == 1


def test_heartbeats_fire_at_each_deadline_during_long_reopen_wait(
    monkeypatch, tmp_path, capsys
):
    clock = FakeClock()
    stop_event = AdvancingStopEvent(clock)
    worker = ScriptedWorker([WorkerTimeout("trigger hung")])
    monkeypatch.setattr(soak.time, "monotonic", clock.monotonic)
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            SoakConfig(
                interval=10,
                reopen_interval=10,
                heartbeat_interval=1,
                duration=3.5,
            ),
            event_log,
            worker_factory=lambda worker_config: worker,
        )
        runner.stop_event = stop_event
        runner.run()

    heartbeats = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("heartbeat ")
    ]
    assert heartbeats == [
        "heartbeat elapsed=1.000s attempted_triggers=1",
        "heartbeat elapsed=2.000s attempted_triggers=1",
        "heartbeat elapsed=3.000s attempted_triggers=1",
    ]
    assert clock.now == 3.5
    assert [
        name
        for name in marker_names(tmp_path / "run.csv")
        if name.startswith("reopen_")
    ] == []


def test_missed_heartbeat_deadlines_emit_once_without_catch_up_burst(
    monkeypatch, tmp_path, capsys
):
    """Would catch repeated immediate heartbeats after one delayed callback."""
    clock = FakeClock()
    clock.now = 3.4
    monkeypatch.setattr(soak.time, "monotonic", clock.monotonic)
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            SoakConfig(heartbeat_interval=1),
            event_log,
            worker_factory=lambda worker_config: None,
        )
        runner._started_at = 0
        runner._next_heartbeat = 1
        runner._maybe_heartbeat()
        runner._maybe_heartbeat()

    heartbeats = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("heartbeat ")
    ]
    assert heartbeats == [
        "heartbeat elapsed=3.400s attempted_triggers=0"
    ]


def test_trigger_call_uses_remaining_duration_and_reports_heartbeats(
    monkeypatch, tmp_path, capsys
):
    """Would catch forwarding a long timeout or going silent during a call."""
    clock = FakeClock()
    observed_timeouts = []

    class ProgressWorker(ScriptedWorker):
        def trigger(
            self,
            code,
            flush=True,
            timeout=None,
            progress_callback=None,
            **kwargs,
        ):
            observed_timeouts.append(timeout)
            for now in (0.5, 1.0, 1.1):
                clock.now = now
                if progress_callback is not None:
                    progress_callback()
            return super().trigger(code, flush=flush, timeout=timeout)

    worker = ProgressWorker([successful_result()])
    monkeypatch.setattr(soak.time, "monotonic", clock.monotonic)
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            SoakConfig(
                interval=10,
                call_timeout=20,
                heartbeat_interval=0.5,
                duration=1.25,
                max_triggers=1,
            ),
            event_log,
            worker_factory=lambda worker_config: worker,
        )
        runner.run()

    heartbeats = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("heartbeat ")
    ]
    assert observed_timeouts == [1.25]
    assert heartbeats == [
        "heartbeat elapsed=0.500s attempted_triggers=1",
        "heartbeat elapsed=1.000s attempted_triggers=1",
    ]


def test_stop_event_interrupts_deadline_wait_without_more_work(
    monkeypatch, tmp_path
):
    clock = FakeClock()
    stop_event = InterruptingStopEvent(clock)
    worker = ScriptedWorker([successful_result()])
    monkeypatch.setattr(soak.time, "monotonic", clock.monotonic)
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            SoakConfig(interval=10, heartbeat_interval=1, duration=20),
            event_log,
            worker_factory=lambda worker_config: worker,
        )
        runner.stop_event = stop_event
        runner.run()

    assert stop_event.waits == [0.0, 1.0]
    assert clock.now == 0
    assert len(trigger_rows(tmp_path / "run.csv")) == 1


def test_heartbeats_print_and_append_attempt_count(tmp_path, capsys):
    text_log = tmp_path / "run.log"
    worker = ScriptedWorker([successful_result()] * 2)
    config = SoakConfig(
        interval=0,
        heartbeat_interval=0,
        max_triggers=2,
        text_log=str(text_log),
    )
    run_soak(tmp_path, config, worker)

    output = capsys.readouterr().out
    persisted = text_log.read_text()
    assert "heartbeat elapsed=" in output
    assert "attempted_triggers=2" in output
    assert persisted == output


def test_device_failure_is_loud_in_console_and_text_log(tmp_path, capsys):
    text_log = tmp_path / "run.log"
    failed = ScriptedWorker(
        [
            {
                "ok": False,
                "ready": False,
                "error_code": "DPX_ERR_USB_TEST",
                "error_string": "cable gone",
                "device_time": None,
                "emitted": False,
                "electrically_verified": False,
                "outcome": "device_error",
            }
        ]
    )
    recovered = ScriptedWorker([successful_result()])
    config = SoakConfig(
        interval=0,
        reopen_interval=0,
        max_triggers=2,
        text_log=str(text_log),
    )
    run_soak(tmp_path, config, failed, recovered)

    output = capsys.readouterr().out
    assert "failure outcome=device_error" in output
    assert "error_code=DPX_ERR_USB_TEST" in output
    assert text_log.read_text() == output


def test_failed_reopen_is_loud_in_console_and_text_log(tmp_path, capsys):
    text_log = tmp_path / "run.log"
    failed_trigger = ScriptedWorker([WorkerTimeout("trigger hung")])
    failed_open = ScriptedWorker(
        [], start_result=WorkerProtocolError("reopen pipe failed")
    )
    recovered = ScriptedWorker([successful_result()])
    config = SoakConfig(
        interval=0,
        reopen_interval=0,
        max_triggers=2,
        text_log=str(text_log),
    )

    run_soak(tmp_path, config, failed_trigger, failed_open, recovered)

    output = capsys.readouterr().out
    assert "failure outcome=open_exception" in output
    assert "error_code=WorkerProtocolError" in output
    assert "error_string=reopen pipe failed" in output
    assert text_log.read_text() == output


def test_heartbeats_continue_during_recovery(monkeypatch, tmp_path, capsys):
    class FakeClock:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            return self.now

    class AdvancingStopEvent:
        def __init__(self, clock):
            self.clock = clock

        def is_set(self):
            return False

        def wait(self, seconds):
            self.clock.now += seconds
            return False

    clock = FakeClock()
    failed_trigger = ScriptedWorker([WorkerTimeout("trigger hung")])
    failed_open = ScriptedWorker(
        [], start_result=WorkerProtocolError("reopen pipe failed")
    )
    recovered = ScriptedWorker([successful_result()])
    workers = iter([failed_trigger, failed_open, recovered])
    monkeypatch.setattr(soak.time, "monotonic", clock.monotonic)
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            SoakConfig(
                interval=0,
                reopen_interval=0.6,
                heartbeat_interval=1.0,
                max_triggers=2,
            ),
            event_log,
            worker_factory=lambda worker_config: next(workers),
        )
        runner.stop_event = AdvancingStopEvent(clock)
        runner.run()

    output = capsys.readouterr().out
    assert "heartbeat elapsed=1.000s attempted_triggers=1" in output


def test_heartbeats_continue_while_reopen_call_is_blocked(
    monkeypatch, tmp_path, capsys
):
    """Would catch a blocked replacement open suppressing progress heartbeats."""
    clock = FakeClock()
    failed_trigger = ScriptedWorker(
        [
            {
                "ok": False,
                "ready": False,
                "error_code": "DPX_ERR_USB",
                "error_string": "gone",
                "device_time": None,
                "emitted": False,
                "outcome": "device_error",
            }
        ]
    )

    class ProgressOpenWorker(ScriptedWorker):
        def start(self, timeout, progress_callback=None, **kwargs):
            for now in (0.5, 1.0):
                clock.now = now
                if progress_callback is not None:
                    progress_callback()
            return super().start(timeout, **kwargs)

    recovered = ProgressOpenWorker([successful_result()])
    workers = iter([failed_trigger, recovered])
    monkeypatch.setattr(soak.time, "monotonic", clock.monotonic)
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            SoakConfig(
                interval=10,
                reopen_interval=0,
                call_timeout=20,
                heartbeat_interval=0.5,
                max_triggers=2,
            ),
            event_log,
            worker_factory=lambda worker_config: next(workers),
        )
        runner.run()

    heartbeats = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("heartbeat ")
    ]
    assert heartbeats == [
        "heartbeat elapsed=0.500s attempted_triggers=1",
        "heartbeat elapsed=1.000s attempted_triggers=1",
    ]


def test_cli_parser_has_required_defaults_and_accepts_every_soak_flag():
    parser = build_parser()
    defaults = parser.parse_args([])
    assert defaults.interval == 1.5
    assert defaults.heartbeat_interval == 60

    args = parser.parse_args(
        [
            "--interval",
            "0.1",
            "--reopen-interval",
            "0.2",
            "--call-timeout",
            "0.3",
            "--heartbeat-interval",
            "0.4",
            "--num-bits",
            "4",
            "--pulse-len",
            "5",
            "--sampling-rate",
            "2000",
            "--csv",
            "events.csv",
            "--text-log",
            "events.log",
            "--max-triggers",
            "7",
            "--duration",
            "8.5",
            "--simulate",
            "--simulate-fail-after-calls",
            "9",
            "--simulate-fail-after-seconds",
            "10.5",
            "--simulate-mode",
            "silent",
            "--simulate-error-code",
            "DPX_ERR_TEST",
            "--no-flush",
        ]
    )
    assert vars(args) == {
        "interval": 0.1,
        "reopen_interval": 0.2,
        "call_timeout": 0.3,
        "heartbeat_interval": 0.4,
        "num_bits": 4,
        "pulse_len": 5,
        "sampling_rate": 2000,
        "csv": "events.csv",
        "text_log": "events.log",
        "max_triggers": 7,
        "duration": 8.5,
        "simulate": True,
        "simulate_fail_after_calls": 9,
        "simulate_fail_after_seconds": 10.5,
        "simulate_mode": "silent",
        "simulate_error_code": "DPX_ERR_TEST",
        "no_flush": True,
    }


def test_simulated_cli_writes_three_sent_rows_and_clean_close(tmp_path):
    csv_path = tmp_path / "cli.csv"
    text_path = tmp_path / "cli.log"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness.soak",
            "--simulate",
            "--interval",
            "0.01",
            "--max-triggers",
            "3",
            "--csv",
            str(csv_path),
            "--text-log",
            str(text_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    rows = csv_rows(csv_path)
    assert [row["outcome"] for row in rows if row["row_type"] == "trigger"] == [
        "sent",
        "sent",
        "sent",
    ]
    assert [row["marker"] for row in rows if row["row_type"] == "marker"][-1] == (
        "worker_closed"
    )


def test_initial_open_hang_stops_at_duration_and_reports_heartbeats(tmp_path):
    """Would catch a short finite run waiting for the longer open timeout."""
    csv_path = tmp_path / "open-hang.csv"
    started = time.monotonic()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness.soak",
            "--simulate",
            "--simulate-fail-after-calls",
            "0",
            "--simulate-mode",
            "hang",
            "--call-timeout",
            "2",
            "--heartbeat-interval",
            "0.05",
            "--duration",
            "0.25",
            "--csv",
            str(csv_path),
            "--text-log",
            str(tmp_path / "open-hang.log"),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == 0, completed.stderr
    assert elapsed < 1.5
    assert "heartbeat " in completed.stdout
    assert "failure outcome=open_exception" in completed.stdout


def test_sigint_cancels_hung_close_before_long_call_timeout(tmp_path):
    """Would catch SIGINT waiting for the full DPxClose call timeout."""
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "harness.soak",
            "--simulate",
            "--simulate-fail-after-calls",
            "9",
            "--simulate-mode",
            "hang",
            "--max-triggers",
            "1",
            "--duration",
            "10",
            "--call-timeout",
            "10",
            "--csv",
            str(tmp_path / "close-hang.csv"),
            "--text-log",
            str(tmp_path / "close-hang.log"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        csv_path = tmp_path / "close-hang.csv"
        trigger_deadline = time.monotonic() + 5
        while time.monotonic() < trigger_deadline:
            if csv_path.exists() and trigger_rows(csv_path):
                break
            time.sleep(0.01)
        else:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=2)
            pytest.fail("simulated trigger did not complete before close")
        interrupted_at = time.monotonic()
        process.send_signal(signal.SIGINT)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=2)
            pytest.fail("SIGINT did not cancel the hung close")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=2)

    assert process.returncode == 0, stderr
    assert time.monotonic() - interrupted_at < 1.5
    assert "worker_close_failed" in marker_names(tmp_path / "close-hang.csv")


def test_sigint_cancels_hung_trigger_without_waiting_for_close(tmp_path):
    """Would catch queueing DPxClose behind a trigger that is still hung."""
    csv_path = tmp_path / "trigger-hang.csv"
    text_path = tmp_path / "trigger-hang.log"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "harness.soak",
            "--simulate",
            "--simulate-fail-after-calls",
            "5",
            "--simulate-mode",
            "hang",
            "--interval",
            "0",
            "--duration",
            "10",
            "--call-timeout",
            "10",
            "--heartbeat-interval",
            "0.05",
            "--csv",
            str(csv_path),
            "--text-log",
            str(text_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        trigger_deadline = time.monotonic() + 5
        while time.monotonic() < trigger_deadline:
            if text_path.exists() and "attempted_triggers=1" in text_path.read_text():
                break
            time.sleep(0.01)
        else:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=2)
            pytest.fail("child did not enter the injected trigger hang")

        assert trigger_rows(csv_path) == []
        interrupted_at = time.monotonic()
        process.send_signal(signal.SIGINT)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=2)
            pytest.fail("SIGINT waited behind the outstanding trigger")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=2)

    assert process.returncode == 0, (stdout, stderr)
    assert time.monotonic() - interrupted_at < 1.5
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)
    rows = csv_rows(csv_path)
    trigger = next(row for row in rows if row["row_type"] == "trigger")
    assert trigger["error_code"] == "WorkerCancelled"
    close_failure = next(
        row for row in rows if row["marker"] == "worker_close_failed"
    )
    assert close_failure["error_code"] == "WorkerForcedTermination"
    assert close_failure["outcome"] == "close_failed"
    assert "outstanding worker request" in close_failure["error_string"]


def test_main_restores_previous_sigint_handler(tmp_path):
    csv_path = tmp_path / "signal.csv"
    text_path = tmp_path / "signal.log"
    original = signal.getsignal(signal.SIGINT)

    def sentinel_handler(signum, frame):
        raise AssertionError("sentinel should not run")

    signal.signal(signal.SIGINT, sentinel_handler)
    try:
        assert (
            main(
                [
                    "--simulate",
                    "--max-triggers",
                    "0",
                    "--csv",
                    str(csv_path),
                    "--text-log",
                    str(text_path),
                ]
            )
            == 0
        )
        assert signal.getsignal(signal.SIGINT) is sentinel_handler
    finally:
        signal.signal(signal.SIGINT, original)


@pytest.mark.parametrize(
    ("failure_flag", "value"),
    [
        ("--simulate-fail-after-calls", "100"),
        ("--simulate-fail-after-seconds", "100"),
    ],
)
def test_soak_cli_requires_duration_for_failure_injection(
    failure_flag, value, tmp_path
):
    """Would catch advertising max-trigger finiteness during startup recovery."""
    with pytest.raises(SystemExit):
        main(
            [
                "--simulate",
                failure_flag,
                value,
                "--max-triggers",
                "0",
                "--csv",
                str(tmp_path / "invalid.csv"),
                "--text-log",
                str(tmp_path / "invalid.log"),
            ]
        )

    assert not (tmp_path / "invalid.csv").exists()
