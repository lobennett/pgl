import multiprocessing

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


def test_close_terminates_child_if_device_close_hangs():
    config = WorkerConfig(
        simulate=True,
        simulation={"fail_after_calls": 5, "failure_mode": "hang"},
    )
    worker = DeviceWorker(config)
    worker.start(timeout=1.0)
    try:
        worker.close(timeout=0.1)
        assert worker.is_alive is False
    finally:
        worker.terminate()


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
