import pytest

from harness.dpx_worker import DeviceWorker, WorkerConfig, WorkerTimeout


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
