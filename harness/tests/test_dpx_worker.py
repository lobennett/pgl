import multiprocessing
import threading

import pytest

from harness import dpx_worker
from harness.dpx_worker import (
    DeviceWorker,
    WorkerConfig,
    WorkerProtocolError,
    WorkerTimeout,
)


class HealthyHardwareBackend:
    def DPxSetDoutSchedule(self, delay, rate, length, address):
        pass

    def DPxStartDoutSched(self):
        pass

    def DPxWriteRegCache(self):
        pass

    def DPxGetTime(self):
        return 1.25

    def DPxGetError(self):
        return "DPX_SUCCESS"

    def DPxGetErrorString(self):
        return "Success"

    def DPxIsReady(self):
        return True

    def DPxClearError(self):
        pass


class RecordingCloseBackend:
    def __init__(self, events, close_exception=None):
        self.events = events
        self.close_exception = close_exception

    def DPxOpen(self):
        pass

    def DPxIsReady(self):
        return True

    def DPxUpdateRegCache(self):
        pass

    def DPxGetTime(self):
        return 1.25

    def DPxGetFirmwareRev(self):
        return 42

    def DPxIsDoutPixelMode(self):
        return False

    def DPxGetError(self):
        return "DPX_SUCCESS"

    def DPxGetErrorString(self):
        return "Success"

    def DPxClearError(self):
        pass

    def DPxWriteRam(self, address, values):
        pass

    def DPxClose(self):
        self.events.append("device_close")
        if self.close_exception is not None:
            raise self.close_exception


class RecordingWorkerConnection:
    def __init__(self, events):
        self.events = events
        self.commands = [{"command": "close"}]
        self.replies = []

    def send(self, reply):
        self.events.append(("send", reply))
        self.replies.append(reply)

    def recv(self):
        return self.commands.pop(0)

    def close(self):
        self.events.append("connection_close")


def test_simulated_worker_opens_and_reports_status():
    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=8))
    try:
        opened = worker.start(timeout=1.0)
        assert opened["ok"] is True
        assert opened["ready"] is True
        assert opened["firmware_revision"] == 42
        assert opened["pixel_mode"] is False
    finally:
        worker.close()


def test_trigger_reports_device_time_error_ready_and_emission():
    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=8))
    try:
        worker.start(timeout=1.0)
        result = worker.trigger(17, timeout=1.0)
        assert result["ok"] is True
        assert result["device_time"] >= 0
        assert result["error_code"] == "DPX_SUCCESS"
        assert result["ready"] is True
        assert result["emitted"] is True
    finally:
        worker.close()


@pytest.mark.parametrize("code", [1, 17, 255])
def test_trigger_reads_expected_samples_from_flat_condition_table(code):
    """Would catch treating a flat RAM write as exact-address-only storage."""
    worker = DeviceWorker(
        WorkerConfig(simulate=True, num_bits=8, pulse_len=3)
    )
    try:
        worker.start(timeout=1.0)
        result = worker.trigger(code, timeout=1.0)
        assert result["samples"] == [code, code, code, 0]
    finally:
        worker.close()


def test_no_flush_is_reported_as_no_emission():
    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=4))
    try:
        worker.start(timeout=1.0)
        result = worker.trigger(3, flush=False, timeout=1.0)
        assert result["ok"] is False
        assert result["outcome"] == "no_emission"
    finally:
        worker.close()


def test_hung_call_times_out_and_worker_can_be_terminated():
    config = WorkerConfig(
        simulate=True,
        simulation={"fail_after_calls": 0, "failure_mode": "hang"},
    )
    worker = DeviceWorker(config)
    with pytest.raises(WorkerTimeout):
        worker.start(timeout=0.1)
    worker.terminate()
    assert worker.is_alive is False


def test_error_result_preserves_exact_device_error():
    config = WorkerConfig(
        simulate=True,
        simulation={
            "fail_after_calls": 0,
            "failure_mode": "error",
            "error_code": "DPX_ERR_USB_TEST",
            "error_string": "test disconnect",
        },
    )
    worker = DeviceWorker(config)
    try:
        opened = worker.start(timeout=1.0)
        assert opened["ok"] is False
        assert opened["error_code"] == "DPX_ERR_USB_TEST"
        assert opened["error_string"] == "test disconnect"
    finally:
        worker.terminate()


def test_silent_result_is_not_mistaken_for_success():
    config = WorkerConfig(
        simulate=True,
        simulation={"fail_after_calls": 7, "failure_mode": "silent"},
        num_bits=2,
    )
    worker = DeviceWorker(config)
    try:
        worker.start(timeout=1.0)
        result = worker.trigger(1, timeout=1.0)
        assert result["ok"] is False
        assert result["ready"] is True
        assert result["error_code"] == "DPX_SUCCESS"
        assert result["emitted"] is False
        assert result["outcome"] == "no_emission"
    finally:
        worker.close()


@pytest.mark.parametrize("code", [-1, 4])
def test_trigger_rejects_code_outside_condition_table(code):
    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=2))
    try:
        worker.start(timeout=1.0)
        with pytest.raises(ValueError, match="code must be between 0 and 3"):
            worker.trigger(code, timeout=1.0)
    finally:
        worker.close()


def test_close_timeout_terminates_child_and_propagates_failure():
    config = WorkerConfig(
        simulate=True,
        simulation={"fail_after_calls": 5, "failure_mode": "hang"},
    )
    worker = DeviceWorker(config)
    worker.start(timeout=1.0)
    try:
        with pytest.raises(WorkerTimeout, match="close"):
            worker.close(timeout=0.1)
        assert worker.is_alive is False
        assert worker._connection is None
        assert worker._process is None
    finally:
        worker.terminate()


def test_child_acknowledges_close_only_after_device_close(monkeypatch):
    """Would catch telling the parent cleanup succeeded before DPxClose ran."""
    events = []
    backend = RecordingCloseBackend(events)
    connection = RecordingWorkerConnection(events)
    monkeypatch.setattr(dpx_worker, "_backend", lambda config: backend)

    dpx_worker._worker_main(connection, WorkerConfig(num_bits=1))

    close_ack = next(
        event
        for event in events
        if isinstance(event, tuple) and event[1].get("outcome") == "closed"
    )
    assert events.index("device_close") < events.index(close_ack)


def test_child_reports_device_close_exception_instead_of_success(monkeypatch):
    """Would catch swallowing a backend close exception after a success ack."""
    events = []
    backend = RecordingCloseBackend(
        events,
        close_exception=RuntimeError("DPxClose exploded"),
    )
    connection = RecordingWorkerConnection(events)
    monkeypatch.setattr(dpx_worker, "_backend", lambda config: backend)

    dpx_worker._worker_main(connection, WorkerConfig(num_bits=1))

    close_replies = [
        reply
        for reply in connection.replies
        if reply.get("outcome") == "closed" or "close_error" in reply
    ]
    assert close_replies == [{"close_error": "RuntimeError: DPxClose exploded"}]


def test_worker_propagates_injected_device_close_exception():
    """Would catch turning a child DPxClose exception into graceful cleanup."""
    config = WorkerConfig(
        simulate=True,
        simulation={
            "fail_after_calls": 5,
            "failure_mode": "exception",
            "error_string": "simulated device call exception",
        },
    )
    worker = DeviceWorker(config)
    worker.start(timeout=1.0)

    with pytest.raises(RuntimeError, match="simulated device call exception"):
        worker.close(timeout=1.0)

    assert worker._connection is None
    assert worker._process is None


def test_acknowledged_close_that_needs_termination_propagates_failure():
    """Would catch treating a forced child termination as graceful close."""
    class CloseAckConnection:
        closed = False

        def send(self, command):
            assert command == {"command": "close"}

        def poll(self, timeout):
            return True

        def recv(self):
            return {"ok": True, "outcome": "closed"}

        def close(self):
            self.closed = True

    class StubbornProcess:
        def __init__(self):
            self.alive = True
            self._closed = False

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            pass

        def terminate(self):
            self.alive = False

        def kill(self):
            self.alive = False

        def close(self):
            self._closed = True

    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=1))
    worker._connection = CloseAckConnection()
    worker._process = StubbornProcess()

    with pytest.raises(RuntimeError, match="forced termination"):
        worker.close(timeout=0)

    assert worker._connection is None
    assert worker._process is None


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (WorkerConfig(num_bits=0), "num_bits must be between 1 and 24"),
        (WorkerConfig(num_bits=25), "num_bits must be between 1 and 24"),
        (WorkerConfig(pulse_len=0), "pulse_len must be at least 1"),
        (WorkerConfig(sampling_rate=0), "sampling_rate must be greater than 0"),
        (
            WorkerConfig(ram_base_address=-1),
            "ram_base_address must be non-negative",
        ),
        (
            WorkerConfig(num_bits=23, pulse_len=1),
            "estimated condition table allocation exceeds 256 MiB",
        ),
        (
            WorkerConfig(ram_base_address=0xFFFFFFFF),
            "condition table address range must fit in unsigned 32-bit space",
        ),
        (
            WorkerConfig(num_bits=1, pulse_len=1, ram_base_address=0xFFFFFFFE),
            "condition table address range must fit in unsigned 32-bit space",
        ),
        (
            WorkerConfig(num_bits=24, pulse_len=128),
            "condition table address range must fit in unsigned 32-bit space",
        ),
    ],
)
def test_invalid_config_is_rejected_in_parent_before_start(config, message):
    with pytest.raises(ValueError, match=message):
        DeviceWorker(config)


def test_normal_eight_bit_config_is_accepted_in_parent():
    config = WorkerConfig(simulate=True, num_bits=8)
    worker = DeviceWorker(config)
    assert worker.config is config


def test_closed_pipe_send_is_reported_as_protocol_error_and_cleanup_remains_safe():
    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=2))
    worker.start(timeout=1.0)
    worker._connection.close()
    try:
        with pytest.raises(WorkerProtocolError, match="worker pipe"):
            worker.trigger(1, timeout=1.0)
    finally:
        worker.terminate()
    assert worker._connection is None
    assert worker._process is None


def test_closed_pipe_poll_is_reported_as_protocol_error_and_cleanup_remains_safe():
    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=2))
    worker.start(timeout=1.0)
    worker._connection.close()
    try:
        with pytest.raises(WorkerProtocolError, match="worker pipe"):
            worker._receive(0)
    finally:
        worker.terminate()
    assert worker._connection is None
    assert worker._process is None


def test_dead_child_is_protocol_error_and_termination_clears_handles():
    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=2))
    worker.start(timeout=1.0)
    process = worker._process
    process.terminate()
    process.join(1.0)
    with pytest.raises(WorkerProtocolError, match="worker is not running"):
        worker.trigger(1, timeout=1.0)
    worker.terminate()
    assert worker._connection is None
    assert worker._process is None


def test_normal_close_clears_handles_and_is_idempotent():
    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=2))
    worker.start(timeout=1.0)
    connection = worker._connection
    process = worker._process

    worker.close(timeout=1.0)

    assert worker._connection is None
    assert worker._process is None
    assert connection.closed
    assert process._closed
    worker.close()
    worker.terminate()


def test_terminate_clears_handles_and_is_idempotent():
    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=2))
    worker.start(timeout=1.0)
    connection = worker._connection
    process = worker._process

    worker.terminate()

    assert worker._connection is None
    assert worker._process is None
    assert connection.closed
    assert process._closed
    worker.terminate()
    worker.close()


def test_start_failure_closes_both_pipe_ends_and_process_handle(monkeypatch):
    real_context = multiprocessing.get_context("spawn")
    connections = []
    processes = []

    class TrackingContext:
        def Pipe(self, duplex):
            pair = real_context.Pipe(duplex=duplex)
            connections.extend(pair)
            return pair

        def Process(self, *args, **kwargs):
            process = real_context.Process(*args, **kwargs)
            processes.append(process)
            return process

    def fail_start(self):
        raise RuntimeError("start failed")

    monkeypatch.setattr(
        dpx_worker.multiprocessing,
        "get_context",
        lambda _: TrackingContext(),
    )
    monkeypatch.setattr(multiprocessing.process.BaseProcess, "start", fail_start)
    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=2))
    try:
        with pytest.raises(RuntimeError, match="start failed"):
            worker.start(timeout=1.0)
        assert worker._connection is None
        assert worker._process is None
        assert all(connection.closed for connection in connections)
        assert processes[0]._closed
        worker.close()
        worker.terminate()
    finally:
        for connection in connections:
            if not connection.closed:
                connection.close()
        for process in processes:
            if not process._closed:
                process.close()


def test_healthy_real_backend_is_labeled_flushed_but_electrically_unverified():
    result = dpx_worker._trigger_device(
        HealthyHardwareBackend(),
        WorkerConfig(simulate=False, num_bits=2),
        {
            "base_address": 8_000_000,
            "bytes_per_condition": 8,
            "samples_per_condition": 4,
        },
        {"code": 1, "flush": True},
    )

    assert result["ok"] is True
    assert result["outcome"] == "flushed_unverified"
    assert result["emitted"] is None
    assert result["electrically_verified"] is False


def test_receive_polls_in_bounded_slices_and_reports_progress():
    """Would catch one long pipe poll suppressing cancellation and heartbeats."""
    class DelayedReplyConnection:
        def __init__(self):
            self.elapsed = 0.0
            self.poll_timeouts = []

        def poll(self, timeout):
            self.poll_timeouts.append(timeout)
            self.elapsed += timeout
            return self.elapsed >= 0.16

        def recv(self):
            return {"ok": True}

    connection = DelayedReplyConnection()
    progress = []
    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=1))
    worker._connection = connection

    assert worker._receive(
        1.0,
        progress_callback=lambda: progress.append(connection.elapsed),
    ) == {"ok": True}
    assert len(connection.poll_timeouts) > 1
    assert max(connection.poll_timeouts) <= 0.1
    assert len(progress) == len(connection.poll_timeouts) - 1


def test_receive_cancellation_stops_before_long_timeout():
    """Would catch a stop request waiting for the original call timeout."""
    cancel_event = threading.Event()

    class CancellingConnection:
        elapsed = 0.0

        def poll(self, timeout):
            self.elapsed += timeout
            cancel_event.set()
            return False

    connection = CancellingConnection()
    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=1))
    worker._connection = connection

    with pytest.raises(WorkerProtocolError, match="cancelled"):
        worker._receive(30.0, cancel_event=cancel_event)
    assert connection.elapsed <= 0.1


def test_close_wait_is_cancellable_and_cleans_process_resources():
    """Would catch close ignoring stop while DPxClose is hung."""
    cancel_event = threading.Event()
    progress_calls = []
    config = WorkerConfig(
        simulate=True,
        simulation={"fail_after_calls": 5, "failure_mode": "hang"},
    )
    worker = DeviceWorker(config)
    worker.start(timeout=1.0)

    def report_progress():
        progress_calls.append(None)
        if len(progress_calls) == 2:
            cancel_event.set()

    try:
        with pytest.raises(WorkerProtocolError, match="cancelled"):
            worker.close(
                timeout=30.0,
                cancel_event=cancel_event,
                progress_callback=report_progress,
            )
    finally:
        worker.terminate()

    assert len(progress_calls) == 2
    assert worker._connection is None
    assert worker._process is None
