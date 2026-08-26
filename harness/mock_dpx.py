"""In-process, stateful replacement for :mod:`pypixxlib._libdpx`."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, RLock


part_number_constants = {3: "DATAPixx3"}


@dataclass
class _MockState:
    opened: bool = False
    opened_at: float | None = None
    error_code: str = "DPX_SUCCESS"
    error_string: str = "Success"
    pending_ram: dict[int, list[int]] = field(default_factory=dict)
    committed_ram: dict[int, list[int]] = field(default_factory=dict)
    pending_schedule: tuple[float, int, int, int] | None = None
    schedule_started: bool = False
    emissions: list[dict[str, object]] = field(default_factory=list)
    call_count: int = 0
    persistent_handle_path: Path | None = None
    owns_persistent_handle: bool = False
    fail_after_calls: int | None = None
    fail_after_seconds: float | None = None
    failure_mode: str = "error"
    configured_error_code: str = "DPX_ERR_USB"
    configured_error_string: str = "Simulated USB failure"
    fault_latched: bool = False
    commits_dropped: bool = False
    pixel_mode: bool = False
    din_logging: bool = False
    dout_buffer: tuple[int, int] | None = None


_lock = RLock()
_hang_entered = Event()
_hang_released = Event()
_state = _MockState()


def configure(
    *,
    fail_after_calls=None,
    fail_after_seconds=None,
    failure_mode="error",
    error_code="DPX_ERR_USB",
    error_string="Simulated USB failure",
    persistent_handle_path=None,
) -> None:
    """Configure simulated connection options for a subsequent test run."""
    with _lock:
        _state.fail_after_calls = fail_after_calls
        _state.fail_after_seconds = fail_after_seconds
        _state.failure_mode = failure_mode
        _state.configured_error_code = error_code
        _state.configured_error_string = error_string
        _state.persistent_handle_path = (
            Path(persistent_handle_path) if persistent_handle_path is not None else None
        )


def reset_mock(*, remove_persistent_handle=True) -> None:
    """Return the fake to its initial state and optionally remove its marker."""
    global _state
    with _lock:
        marker = _state.persistent_handle_path
        owned = _state.owns_persistent_handle
        if remove_persistent_handle and marker is not None and owned:
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
        _state = _MockState()
        _hang_released.set()
        _hang_released.clear()


def release_hang() -> None:
    """Unblock a future hang-mode fault injection call."""
    _hang_released.set()


def get_mock_state() -> dict:
    """Return a snapshot of simulation state for emission assertions."""
    with _lock:
        return {
            "opened": _state.opened,
            "opened_at": _state.opened_at,
            "error_code": _state.error_code,
            "error_string": _state.error_string,
            "pending_ram": {key: value.copy() for key, value in _state.pending_ram.items()},
            "committed_ram": {key: value.copy() for key, value in _state.committed_ram.items()},
            "pending_schedule": _state.pending_schedule,
            "schedule_started": _state.schedule_started,
            "emissions": [emission.copy() for emission in _state.emissions],
            "call_count": _state.call_count,
            "fault_latched": _state.fault_latched,
            "commits_dropped": _state.commits_dropped,
            "hang_entered": _hang_entered.is_set(),
            "persistent_handle_path": _state.persistent_handle_path,
            "owns_persistent_handle": _state.owns_persistent_handle,
        }


def _threshold_reached(state, now):
    calls_reached = (
        state.fail_after_calls is not None
        and state.call_count >= state.fail_after_calls
    )
    time_reached = (
        state.fail_after_seconds is not None
        and state.opened_at is not None
        and now - state.opened_at >= state.fail_after_seconds
    )
    return calls_reached or time_reached


def _before_call(_name: str) -> None:
    with _lock:
        if _name == "DPxIsReady" or not _state.opened:
            return
        if not _state.fault_latched:
            if _threshold_reached(_state, time.monotonic()):
                _state.fault_latched = True
                if _state.failure_mode == "error":
                    _set_error(_state.configured_error_code, _state.configured_error_string)
                elif _state.failure_mode == "silent":
                    _state.commits_dropped = True
            else:
                _state.call_count += 1
                return
        should_hang = _state.failure_mode == "hang"
    if should_hang:
        _hang_entered.set()
        try:
            _hang_released.wait()
        finally:
            _hang_entered.clear()


def _set_error(code: str, message: str) -> None:
    _state.error_code = code
    _state.error_string = message


def DPxOpen():
    _before_call("DPxOpen")
    with _lock:
        if _state.fault_latched and _state.failure_mode == "error":
            return
        marker = _state.persistent_handle_path
        if _state.opened:
            _set_error("DPX_ERR_ALREADY_OPEN", "DATAPixx connection is already open")
            return
        if marker is not None:
            try:
                fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                _set_error("DPX_ERR_ALREADY_OPEN", "DATAPixx connection is already open")
                return
            else:
                os.close(fd)
                _state.owns_persistent_handle = True
        _state.opened = True
        _state.opened_at = time.monotonic()


def DPxClose():
    _before_call("DPxClose")
    with _lock:
        if _state.owns_persistent_handle and _state.persistent_handle_path is not None:
            try:
                _state.persistent_handle_path.unlink()
            except FileNotFoundError:
                pass
        _state.opened = False
        _state.opened_at = None
        _state.owns_persistent_handle = False


def DPxIsReady():
    _before_call("DPxIsReady")
    with _lock:
        return _state.opened


def DPxGetError():
    with _lock:
        return _state.error_code


def DPxGetErrorString():
    with _lock:
        return _state.error_string


def DPxClearError():
    with _lock:
        if not (_state.fault_latched and _state.failure_mode == "error"):
            _set_error("DPX_SUCCESS", "Success")


def DPxUpdateRegCache():
    DPxWriteRegCache()


def DPxWriteRegCache():
    _before_call("DPxWriteRegCache")
    with _lock:
        if _state.commits_dropped:
            _state.pending_ram.clear()
            _state.schedule_started = False
            return
        _state.committed_ram.update(
            {address: values.copy() for address, values in _state.pending_ram.items()}
        )
        _state.pending_ram.clear()
        if _state.schedule_started and _state.pending_schedule is not None:
            _, rate, length, address = _state.pending_schedule
            _state.emissions.append(
                {
                    "address": address,
                    "rate": rate,
                    "length": length,
                    "samples": _state.committed_ram.get(address, [])[:length],
                }
            )
            _state.schedule_started = False


def DPxWriteRam(address, values):
    _before_call("DPxWriteRam")
    with _lock:
        _state.pending_ram[int(address)] = [int(value) for value in values]


def DPxSetDoutSchedule(delay, rate, length, address):
    _before_call("DPxSetDoutSchedule")
    with _lock:
        _state.pending_schedule = (delay, rate, length, address)


def DPxSetDoutSched(delay, rate, _units, length):
    _before_call("DPxSetDoutSched")
    with _lock:
        address = _state.dout_buffer[0] if _state.dout_buffer is not None else 0
        _state.pending_schedule = (delay, rate, length, address)


def DPxSetDoutSchedRate(_rate, _units):
    _before_call("DPxSetDoutSchedRate")


def DPxStartDoutSched():
    _before_call("DPxStartDoutSched")
    with _lock:
        _state.schedule_started = True


def DPxGetTime():
    _before_call("DPxGetTime")
    return time.monotonic()


def DPxGetFirmwareRev():
    _before_call("DPxGetFirmwareRev")
    return 42


def DPxIs5VFault():
    _before_call("DPxIs5VFault")
    return False


def DPxIsDoutPixelMode():
    _before_call("DPxIsDoutPixelMode")
    with _lock:
        return _state.pixel_mode


def DPxIsDacSchedRunning():
    _before_call("DPxIsDacSchedRunning")
    return False


def _set_pixel_mode(enabled):
    _before_call("pixel_mode")
    with _lock:
        _state.pixel_mode = enabled


def DPxDisableDoutPixelMode():
    _set_pixel_mode(False)


def DPxDisableDoutPixelModeB():
    _set_pixel_mode(False)


def DPxDisableDoutPixelModeGB():
    _set_pixel_mode(False)


def DPxEnableDoutPixelMode():
    _set_pixel_mode(True)


def DPxEnableDoutPixelModeB():
    _set_pixel_mode(True)


def DPxEnableDoutPixelModeGB():
    _set_pixel_mode(True)


def DPxStopAllScheds():
    _before_call("DPxStopAllScheds")
    with _lock:
        _state.schedule_started = False


def DPxSelectDevice(_device):
    _before_call("DPxSelectDevice")


def DPxReadProductionInfo():
    _before_call("DPxReadProductionInfo")
    return {"Assembly REV": "MOCK-A", "S/N": "MOCK-0001"}


def DPxGetPartNumber():
    _before_call("DPxGetPartNumber")
    return 3


def DPxEnableDinDebounce():
    _before_call("DPxEnableDinDebounce")


def DPxEnableDoutButtonSchedules():
    _before_call("DPxEnableDoutButtonSchedules")


def DPxSetDoutButtonSchedulesMode(_mode):
    _before_call("DPxSetDoutButtonSchedulesMode")


def DPxSetDinLog(_address, _frames):
    _before_call("DPxSetDinLog")


def DPxStartDinLog():
    _before_call("DPxStartDinLog")
    with _lock:
        _state.din_logging = True


def DPxGetDinStatus(status):
    _before_call("DPxGetDinStatus")
    status.update({"newLogFrames": 0, "currentReadFrame": 0})


def DPxReadDinLog(_status, _new_events):
    _before_call("DPxReadDinLog")
    return []


def DPxGetDoutBuffBaseAddr():
    _before_call("DPxGetDoutBuffBaseAddr")
    return 8_000_000


def DPxGetDoutNumBits():
    _before_call("DPxGetDoutNumBits")
    return 24


def DPxSetDoutBuff(address, size):
    _before_call("DPxSetDoutBuff")
    with _lock:
        _state.dout_buffer = (address, size)
