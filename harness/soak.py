"""Display-free DATAPixx trigger soak and recovery runner."""

from __future__ import annotations

import argparse
import math
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from harness.csv_log import CsvEventLog
from harness.dpx_worker import (
    DeviceWorker,
    WorkerConfig,
    WorkerProtocolError,
    WorkerTimeout,
)


@dataclass(frozen=True)
class SoakConfig:
    interval: float = 1.5
    reopen_interval: float = 5.0
    call_timeout: float = 2.0
    heartbeat_interval: float = 60.0
    num_bits: int = 8
    pulse_len: int = 3
    sampling_rate: int = 1000
    max_triggers: int | None = None
    duration: float | None = None
    simulate: bool = False
    simulate_fail_after_calls: int | None = None
    simulate_fail_after_seconds: float | None = None
    simulate_mode: str = "error"
    simulate_error_code: str = "DPX_ERR_USB"
    flush_triggers: bool = True
    text_log: str | None = None


def trigger_code(sequence_number, num_bits):
    max_code = (1 << num_bits) - 1
    return ((sequence_number - 1) % max_code) + 1


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run a display-free DATAPixx trigger soak test."
    )
    parser.add_argument("--interval", type=float, default=1.5)
    parser.add_argument("--reopen-interval", type=float, default=5.0)
    parser.add_argument("--call-timeout", type=float, default=2.0)
    parser.add_argument("--heartbeat-interval", type=float, default=60)
    parser.add_argument("--num-bits", type=int, default=8)
    parser.add_argument("--pulse-len", type=int, default=3)
    parser.add_argument("--sampling-rate", type=int, default=1000)
    parser.add_argument("--csv", default="dpx-soak.csv")
    parser.add_argument("--text-log", default="dpx-soak.log")
    parser.add_argument("--max-triggers", type=int)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--simulate-fail-after-calls", type=int)
    parser.add_argument("--simulate-fail-after-seconds", type=float)
    parser.add_argument(
        "--simulate-mode",
        choices=("error", "hang", "silent"),
        default="error",
    )
    parser.add_argument("--simulate-error-code", default="DPX_ERR_USB")
    parser.add_argument("--no-flush", action="store_true")
    return parser


class SoakRunner:
    def __init__(self, config, event_log, worker_factory=DeviceWorker):
        self.config = config
        self.event_log = event_log
        self.worker_factory = worker_factory
        self.stop_event = threading.Event()
        self.worker = None
        self.sequence_number = 0
        self._started_at = None
        self._duration_deadline = None
        self._next_heartbeat = None

    def _emit_text(self, message):
        line = f"{message}\n"
        print(message, flush=True)
        if self.config.text_log is not None:
            with Path(self.config.text_log).open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())

    def _emit_failure(self, result):
        self._emit_text(
            "failure "
            f"outcome={result.get('outcome', 'unknown')} "
            f"error_code={result.get('error_code', '')} "
            f"error_string={result.get('error_string', '')}"
        )

    def _maybe_heartbeat(self):
        now = time.monotonic()
        if now < self._next_heartbeat:
            return
        self._emit_text(
            f"heartbeat elapsed={now - self._started_at:.3f}s "
            f"attempted_triggers={self.sequence_number}"
        )
        if self.config.heartbeat_interval > 0:
            missed_intervals = int(
                (now - self._next_heartbeat)
                // self.config.heartbeat_interval
            ) + 1
            self._next_heartbeat += (
                missed_intervals * self.config.heartbeat_interval
            )
        else:
            self._next_heartbeat = now

    def _worker_config(self):
        simulation = {
            "fail_after_calls": self.config.simulate_fail_after_calls,
            "fail_after_seconds": self.config.simulate_fail_after_seconds,
            "failure_mode": self.config.simulate_mode,
            "error_code": self.config.simulate_error_code,
        }
        return WorkerConfig(
            simulate=self.config.simulate,
            simulation=simulation,
            num_bits=self.config.num_bits,
            pulse_len=self.config.pulse_len,
            sampling_rate=self.config.sampling_rate,
        )

    def _duration_reached(self, now=None):
        if self._duration_deadline is None:
            return False
        if now is None:
            now = time.monotonic()
        return now >= self._duration_deadline

    def _effective_call_timeout(self):
        timeout = self.config.call_timeout
        if self._duration_deadline is None:
            return timeout
        remaining = max(0.0, self._duration_deadline - time.monotonic())
        return min(timeout, remaining)

    def _wait_until(self, operation_deadline):
        while True:
            if self.stop_event.is_set():
                return False
            now = time.monotonic()
            if self._duration_reached(now):
                return False
            if now >= operation_deadline:
                return not self.stop_event.wait(0)

            wake_deadlines = [operation_deadline]
            if self._duration_deadline is not None:
                wake_deadlines.append(self._duration_deadline)
            if self.config.heartbeat_interval > 0:
                wake_deadlines.append(self._next_heartbeat)
            wake_deadline = min(wake_deadlines)
            if self.stop_event.wait(max(0.0, wake_deadline - now)):
                return False

            now = time.monotonic()
            if self._duration_reached(now):
                return False
            if now >= operation_deadline:
                return True
            if (
                self.config.heartbeat_interval > 0
                and now >= self._next_heartbeat
            ):
                self._maybe_heartbeat()

    def _run_limit_reached(self):
        return (
            self.config.max_triggers is not None
            and self.sequence_number >= self.config.max_triggers
        ) or self._duration_reached()

    def _advance_trigger_deadline(self, previous_deadline):
        next_deadline = previous_deadline + self.config.interval
        if self.config.interval <= 0:
            return next_deadline
        behind = time.monotonic() - next_deadline
        if behind > 0:
            next_deadline += (
                math.ceil(behind / self.config.interval)
                * self.config.interval
            )
        return next_deadline

    @staticmethod
    def _opened_successfully(result):
        return (
            result.get("ok") is True
            and result.get("ready") is True
            and result.get("error_code") == "DPX_SUCCESS"
        )

    @staticmethod
    def _trigger_succeeded(result):
        return (
            result.get("ok") is True
            and result.get("ready") is True
            and result.get("error_code") == "DPX_SUCCESS"
            and result.get("emitted") is not False
            and result.get("outcome") not in {
                "device_error",
                "no_emission",
                "uncommitted",
            }
        )

    @staticmethod
    def _exception_result(exception):
        if isinstance(exception, WorkerTimeout):
            outcome = "worker_timeout"
        elif isinstance(exception, WorkerProtocolError):
            outcome = "worker_protocol_error"
        else:
            outcome = "worker_exception"
        return {
            "ok": False,
            "ready": False,
            "error_code": type(exception).__name__,
            "error_string": str(exception),
            "device_time": None,
            "outcome": outcome,
        }

    def _new_started_worker(self):
        worker = self.worker_factory(self._worker_config())
        try:
            result = worker.start(
                timeout=self._effective_call_timeout(),
                cancel_event=self.stop_event,
                progress_callback=self._maybe_heartbeat,
            )
        except Exception:
            worker.terminate()
            raise
        if not self._opened_successfully(result):
            worker.terminate()
            return None, result
        return worker, result

    def _recover(self):
        if self.worker is not None:
            self.worker.terminate()
            self.worker = None

        while not self.stop_event.is_set() and not self._duration_reached():
            reopen_deadline = time.monotonic() + self.config.reopen_interval
            if not self._wait_until(reopen_deadline):
                return False
            self.event_log.write_marker("reopen_attempt")
            candidate = None
            try:
                candidate, result = self._new_started_worker()
            except Exception as exception:
                failure = self._exception_result(exception)
                failure["outcome"] = "open_exception"
                self.event_log.write_marker(
                    "reopen_failed",
                    f"{type(exception).__name__}: {exception}",
                    error_code=type(exception).__name__,
                    error_string=str(exception),
                    ready=False,
                    outcome="open_exception",
                )
                self._emit_failure(failure)
                continue
            if candidate is None:
                failure = dict(result)
                failure["outcome"] = "open_failed"
                self.event_log.write_marker(
                    "reopen_failed",
                    result.get("error_string", ""),
                    device_time=result.get("device_time"),
                    error_code=result.get("error_code", ""),
                    error_string=result.get("error_string", ""),
                    ready=result.get("ready", False),
                    outcome="open_failed",
                )
                self._emit_failure(failure)
                continue
            self.worker = candidate
            self.event_log.write_marker(
                "reopen_succeeded",
                device_time=result.get("device_time"),
                error_code=result.get("error_code", ""),
                error_string=result.get("error_string", ""),
                ready=result.get("ready", False),
                outcome="opened",
            )
            return True
        return False

    def _write_trigger(self, code, result, latency_ms):
        self.event_log.write_trigger(
            device_time=result.get("device_time"),
            sequence_number=self.sequence_number,
            trigger_code=code,
            error_code=result.get("error_code", ""),
            error_string=result.get("error_string", ""),
            ready=result.get("ready", False),
            latency_ms=latency_ms,
            outcome=result.get("outcome", "unknown"),
            detail=result.get("detail", ""),
        )

    def run(self):
        self._started_at = time.monotonic()
        self._duration_deadline = (
            self._started_at + self.config.duration
            if self.config.duration is not None
            else None
        )
        self._next_heartbeat = (
            self._started_at + self.config.heartbeat_interval
        )
        next_trigger = self._started_at
        try:
            try:
                self.worker, opened = self._new_started_worker()
            except Exception as exception:
                self.worker = None
                opened = self._exception_result(exception)
                opened["outcome"] = "open_exception"
            if self.worker is None:
                self.event_log.write_marker(
                    "open_failed",
                    opened.get("error_string", ""),
                    error_code=opened.get("error_code", ""),
                    error_string=opened.get("error_string", ""),
                    ready=opened.get("ready", False),
                    outcome="open_failed",
                )
                self._emit_failure(opened)
                if not self._recover():
                    return
                next_trigger = time.monotonic()

            while not self.stop_event.is_set() and not self._run_limit_reached():
                if not self._wait_until(next_trigger):
                    break

                self.sequence_number += 1
                code = trigger_code(self.sequence_number, self.config.num_bits)
                call_started = time.monotonic()
                try:
                    result = self.worker.trigger(
                        code,
                        flush=self.config.flush_triggers,
                        timeout=self._effective_call_timeout(),
                        cancel_event=self.stop_event,
                        progress_callback=self._maybe_heartbeat,
                    )
                except Exception as exception:
                    result = self._exception_result(exception)
                latency_ms = (time.monotonic() - call_started) * 1000.0
                self._write_trigger(code, result, latency_ms)
                self._maybe_heartbeat()

                if not self._trigger_succeeded(result):
                    self._emit_failure(result)
                    if self._run_limit_reached() or self.stop_event.is_set():
                        break
                    if not self._recover():
                        break
                    next_trigger = time.monotonic()
                else:
                    next_trigger = self._advance_trigger_deadline(next_trigger)
        finally:
            if self.worker is not None:
                try:
                    cancel_event = (
                        None if self.stop_event.is_set() else self.stop_event
                    )
                    self.worker.close(
                        timeout=self._effective_call_timeout(),
                        cancel_event=cancel_event,
                        progress_callback=self._maybe_heartbeat,
                    )
                except Exception as exception:
                    self.worker.terminate()
                    self.event_log.write_marker(
                        "worker_close_failed",
                        f"{type(exception).__name__}: {exception}",
                        error_code=type(exception).__name__,
                        error_string=str(exception),
                        ready=False,
                        outcome="close_failed",
                    )
                else:
                    self.event_log.write_marker("worker_closed")
                self.worker = None


def _config_from_args(args):
    return SoakConfig(
        interval=args.interval,
        reopen_interval=args.reopen_interval,
        call_timeout=args.call_timeout,
        heartbeat_interval=args.heartbeat_interval,
        num_bits=args.num_bits,
        pulse_len=args.pulse_len,
        sampling_rate=args.sampling_rate,
        max_triggers=args.max_triggers,
        duration=args.duration,
        simulate=args.simulate,
        simulate_fail_after_calls=args.simulate_fail_after_calls,
        simulate_fail_after_seconds=args.simulate_fail_after_seconds,
        simulate_mode=args.simulate_mode,
        simulate_error_code=args.simulate_error_code,
        flush_triggers=not args.no_flush,
        text_log=args.text_log,
    )


def _close_runner_worker(runner):
    worker = runner.worker
    if worker is None:
        return
    try:
        worker.close(timeout=runner.config.call_timeout)
    except Exception:
        worker.terminate()
    finally:
        runner.worker = None


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    failure_injection_requested = (
        args.simulate_fail_after_calls is not None
        or args.simulate_fail_after_seconds is not None
    )
    if failure_injection_requested and args.duration is None:
        parser.error(
            "--duration is required with simulation failure injection flags"
        )
    event_log = CsvEventLog(args.csv)
    runner = SoakRunner(_config_from_args(args), event_log)

    def request_stop(_signum, _frame):
        runner.stop_event.set()

    previous_handler = None
    try:
        previous_handler = signal.signal(signal.SIGINT, request_stop)
        runner.run()
    finally:
        try:
            _close_runner_worker(runner)
            event_log.close()
        finally:
            if previous_handler is not None:
                signal.signal(signal.SIGINT, previous_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
