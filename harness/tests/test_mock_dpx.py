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
