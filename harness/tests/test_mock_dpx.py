import threading

import pytest

from harness import mock_dpx


def setup_function():
    mock_dpx.reset_mock()


def test_open_close_and_duplicate_open(tmp_path):
    """Would catch opening an already-open connection without an error."""
    mock_dpx.configure()
    mock_dpx.DPxOpen()
    assert mock_dpx.DPxIsReady() is True

    mock_dpx.DPxOpen()
    assert mock_dpx.DPxGetError() == "DPX_ERR_ALREADY_OPEN"

    mock_dpx.DPxClearError()
    mock_dpx.DPxClose()
    assert mock_dpx.DPxIsReady() is False


def test_persistent_handle_survives_reset_when_requested(tmp_path):
    """Would catch reset deleting a simulated handle that must remain stale."""
    marker = tmp_path / "driver-handle"
    mock_dpx.configure(persistent_handle_path=marker)
    mock_dpx.DPxOpen()
    assert marker.exists()

    mock_dpx.reset_mock(remove_persistent_handle=False)
    mock_dpx.configure(persistent_handle_path=marker)
    mock_dpx.DPxOpen()
    assert mock_dpx.DPxGetError() == "DPX_ERR_ALREADY_OPEN"


def test_schedule_emits_only_when_register_cache_is_written():
    """Would catch emitting a trigger before staged registers are committed."""
    mock_dpx.configure()
    mock_dpx.DPxOpen()
    mock_dpx.DPxWriteRam(8_000_000, [7, 7, 0])
    mock_dpx.DPxSetDoutSchedule(0.0, 1000, 3, 8_000_000)
    mock_dpx.DPxStartDoutSched()
    assert mock_dpx.get_mock_state()["emissions"] == []

    mock_dpx.DPxWriteRegCache()
    emissions = mock_dpx.get_mock_state()["emissions"]
    assert len(emissions) == 1
    assert emissions[0]["samples"] == [7, 7, 0]


def test_ram_write_keeps_samples_in_one_compact_contiguous_blob():
    """Would catch restoring one dictionary entry per 16-bit RAM sample."""
    mock_dpx.configure()
    mock_dpx.DPxOpen()
    base_address = 8_000_000
    samples = list(range(4096))

    mock_dpx.DPxWriteRam(base_address, samples)

    pending_ram = mock_dpx.get_mock_state()["pending_ram"]
    assert list(pending_ram) == [base_address]
    assert pending_ram[base_address] == samples


def test_error_activates_after_exact_successful_call_count():
    """Would catch faulting before the configured successful call count."""
    mock_dpx.configure(fail_after_calls=2, error_code="DPX_ERR_TEST")
    mock_dpx.DPxOpen()
    assert mock_dpx.DPxGetFirmwareRev() == 42
    mock_dpx.DPxUpdateRegCache()
    assert mock_dpx.DPxGetError() == "DPX_SUCCESS"
    mock_dpx.DPxWriteRam(0, [1, 0])
    assert mock_dpx.DPxGetError() == "DPX_ERR_TEST"


def test_duplicate_open_preserves_latched_injected_error():
    """Would catch duplicate opening overwriting the latched USB failure."""
    mock_dpx.configure(fail_after_calls=0, error_code="DPX_ERR_TEST")
    mock_dpx.DPxOpen()
    mock_dpx.DPxOpen()
    assert mock_dpx.DPxGetError() == "DPX_ERR_TEST"


def test_elapsed_time_failure_can_start_immediately():
    """Would catch ignoring an elapsed-time threshold of zero seconds."""
    mock_dpx.configure(fail_after_seconds=0, error_code="DPX_ERR_TIMEOUT")
    mock_dpx.DPxOpen()
    mock_dpx.DPxUpdateRegCache()
    assert mock_dpx.DPxGetError() == "DPX_ERR_TIMEOUT"


def test_readiness_does_not_count_toward_failure_threshold():
    """Would catch a diagnostic readiness read consuming a hardware call."""
    mock_dpx.configure(fail_after_calls=1, error_code="DPX_ERR_TEST")
    mock_dpx.DPxOpen()
    assert mock_dpx.DPxIsReady() is True
    assert mock_dpx.DPxGetFirmwareRev() == 42
    assert mock_dpx.DPxGetError() == "DPX_SUCCESS"
    mock_dpx.DPxWriteRam(0, [1, 0])
    assert mock_dpx.DPxGetError() == "DPX_ERR_TEST"


def test_hang_blocks_until_test_releases_it():
    """Would catch releasing a hang before the hardware call reaches its wait."""
    mock_dpx.configure(fail_after_calls=0, failure_mode="hang")
    mock_dpx.DPxOpen()
    thread = threading.Thread(target=mock_dpx.DPxUpdateRegCache, daemon=True)
    thread.start()
    try:
        assert mock_dpx._hang_entered.wait(0.5)
        assert thread.is_alive()
    finally:
        mock_dpx.release_hang()
        thread.join(0.5)
    assert not thread.is_alive()


@pytest.mark.parametrize("hung_operation", ["open", "close"])
def test_diagnostics_and_reset_remain_responsive_while_open_or_close_hangs(
    hung_operation,
):
    """Would catch retaining the mock lock while an open or close waits."""
    mock_dpx.configure(fail_after_calls=0, failure_mode="hang")
    mock_dpx.DPxOpen()
    operation = mock_dpx.DPxOpen if hung_operation == "open" else mock_dpx.DPxClose
    hung_thread = threading.Thread(target=operation, daemon=True)
    hung_thread.start()
    assert mock_dpx._hang_entered.wait(0.5)

    snapshots = []
    inspect_thread = threading.Thread(
        target=lambda: snapshots.append(mock_dpx.get_mock_state()), daemon=True
    )
    reset_thread = threading.Thread(target=mock_dpx.reset_mock, daemon=True)
    inspect_thread.start()
    reset_thread.start()
    try:
        inspect_thread.join(0.1)
        reset_thread.join(0.1)
        assert not inspect_thread.is_alive()
        assert not reset_thread.is_alive()
        assert snapshots
    finally:
        mock_dpx.release_hang()
        hung_thread.join(0.5)
        inspect_thread.join(0.5)
        reset_thread.join(0.5)
    assert not hung_thread.is_alive()


def test_silent_failure_keeps_success_and_drops_emission():
    """Would catch a silent USB fault that still commits an emission."""
    mock_dpx.configure(fail_after_calls=0, failure_mode="silent")
    mock_dpx.DPxOpen()
    mock_dpx.DPxWriteRam(0, [9, 0])
    mock_dpx.DPxSetDoutSchedule(0.0, 1000, 2, 0)
    mock_dpx.DPxStartDoutSched()
    mock_dpx.DPxWriteRegCache()
    assert mock_dpx.DPxGetError() == "DPX_SUCCESS"
    assert mock_dpx.DPxIsReady() is True
    assert mock_dpx.get_mock_state()["emissions"] == []


def test_error_latched_calls_do_not_mutate_trigger_state_or_emit():
    """Would catch error-mode calls continuing to stage or emit a trigger."""
    mock_dpx.configure(
        fail_after_calls=0,
        failure_mode="error",
        error_code="DPX_ERR_TEST",
    )
    mock_dpx.DPxOpen()

    mock_dpx.DPxWriteRam(8_000_000, [9, 9, 0])
    mock_dpx.DPxSetDoutSchedule(0.0, 1000, 3, 8_000_000)
    mock_dpx.DPxStartDoutSched()
    mock_dpx.DPxWriteRegCache()

    state = mock_dpx.get_mock_state()
    assert state["error_code"] == "DPX_ERR_TEST"
    assert state["pending_ram"] == {}
    assert state["committed_ram"] == {}
    assert state["pending_schedule"] is None
    assert state["schedule_started"] is False
    assert state["emissions"] == []
