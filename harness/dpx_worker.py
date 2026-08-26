"""Run DATAPixx calls behind a persistent, spawn-process boundary."""

from __future__ import annotations

import multiprocessing
import sys
import time
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from typing import Any


class WorkerTimeout(TimeoutError):
    """Raised when the device process does not reply before its deadline."""


class WorkerProtocolError(RuntimeError):
    """Raised when the device process sends an invalid reply."""


class WorkerCloseError(RuntimeError):
    """Raised when the child reports that device close failed."""


class WorkerForcedTermination(WorkerCloseError):
    """Raised when the child must be terminated instead of closing gracefully."""


class WorkerCancelled(WorkerProtocolError):
    """Raised when a caller cancels a pending worker reply."""


@dataclass(frozen=True)
class WorkerConfig:
    simulate: bool = False
    simulation: dict[str, Any] = field(default_factory=dict)
    num_bits: int = 8
    pulse_len: int = 3
    sampling_rate: int = 1000
    ram_base_address: int = 8_000_000


_MAX_CONDITION_TABLE_ALLOCATION = 256 * 1024 * 1024
_UINT32_MAX = (1 << 32) - 1
_REPLY_POLL_SLICE = 0.05


def _validate_config(config: WorkerConfig):
    if not 1 <= config.num_bits <= 24:
        raise ValueError("num_bits must be between 1 and 24")
    if config.pulse_len < 1:
        raise ValueError("pulse_len must be at least 1")
    if config.sampling_rate <= 0:
        raise ValueError("sampling_rate must be greater than 0")
    if config.ram_base_address < 0:
        raise ValueError("ram_base_address must be non-negative")

    condition_count = 1 << config.num_bits
    samples_per_condition = config.pulse_len + 1
    table_entry_count = condition_count * samples_per_condition
    table_bytes = table_entry_count * 2
    adjusted_address = config.ram_base_address + config.ram_base_address % 2
    final_address = adjusted_address + table_bytes - 1
    if (
        adjusted_address > _UINT32_MAX
        or table_bytes > _UINT32_MAX
        or final_address > _UINT32_MAX
    ):
        raise ValueError(
            "condition table address range must fit in unsigned 32-bit space"
        )

    list_overhead = sys.getsizeof([])
    list_reference_size = sys.getsizeof([None]) - list_overhead
    integer_size = sys.getsizeof(0)
    estimated_allocation = (
        list_overhead
        + table_entry_count * list_reference_size
        + condition_count * integer_size
    )
    if estimated_allocation > _MAX_CONDITION_TABLE_ALLOCATION:
        raise ValueError(
            "estimated condition table allocation exceeds 256 MiB"
        )


def _condition_layout(num_bits, pulse_len, sampling_rate, base_address):
    if not 1 <= num_bits <= 24:
        raise ValueError("num_bits must be between 1 and 24")
    if base_address % 2:
        base_address += 1
    samples_per_condition = pulse_len + 1
    bytes_per_condition = samples_per_condition * 2
    table = []
    for word in range(2**num_bits):
        table.extend([word] * pulse_len)
        table.append(0)
    return base_address, samples_per_condition, bytes_per_condition, table


def _backend(config: WorkerConfig):
    if config.simulate:
        from harness import mock_dpx

        mock_dpx.configure(**config.simulation)
        return mock_dpx

    from pypixxlib import _libdpx

    return _libdpx


def _error_reply(dp, *, ready, **fields):
    error_code = dp.DPxGetError()
    error_string = dp.DPxGetErrorString()
    reply = {
        **fields,
        "ok": ready and error_code == "DPX_SUCCESS",
        "ready": ready,
        "error_code": error_code,
        "error_string": error_string,
    }
    dp.DPxClearError()
    return reply


def _open_device(dp, config: WorkerConfig):
    dp.DPxOpen()
    ready = dp.DPxIsReady()
    if not ready:
        return _error_reply(dp, ready=ready)

    dp.DPxUpdateRegCache()
    opened = _error_reply(dp, ready=ready)
    if not opened["ok"]:
        return opened

    device_time = dp.DPxGetTime()
    firmware_revision = dp.DPxGetFirmwareRev()
    pixel_mode = dp.DPxIsDoutPixelMode()
    opened = _error_reply(
        dp,
        ready=ready,
        device_time=device_time,
        firmware_revision=firmware_revision,
        pixel_mode=pixel_mode,
    )
    if not opened["ok"]:
        return opened

    base_address, samples, byte_count, table = _condition_layout(
        config.num_bits,
        config.pulse_len,
        config.sampling_rate,
        config.ram_base_address,
    )
    dp.DPxWriteRam(base_address, table)
    opened = _error_reply(
        dp,
        ready=ready,
        device_time=device_time,
        firmware_revision=firmware_revision,
        pixel_mode=pixel_mode,
    )
    opened["layout"] = {
        "base_address": base_address,
        "samples_per_condition": samples,
        "bytes_per_condition": byte_count,
    }
    return opened


def _trigger_device(dp, config: WorkerConfig, layout, command):
    code = command["code"]
    flush = command["flush"]
    before = len(dp.get_mock_state()["emissions"]) if config.simulate else None
    address = layout["base_address"] + layout["bytes_per_condition"] * code
    dp.DPxSetDoutSchedule(
        0.0,
        config.sampling_rate,
        layout["samples_per_condition"],
        address,
    )
    dp.DPxStartDoutSched()
    if flush:
        dp.DPxWriteRegCache()
    device_time = dp.DPxGetTime()
    error_code = dp.DPxGetError()
    error_string = dp.DPxGetErrorString()
    ready = dp.DPxIsReady()
    state = dp.get_mock_state() if config.simulate else None
    emitted = len(state["emissions"]) > before if config.simulate else None
    samples = state["emissions"][-1]["samples"] if emitted else None
    reply = {
        "ok": ready and error_code == "DPX_SUCCESS",
        "ready": ready,
        "error_code": error_code,
        "error_string": error_string,
        "device_time": device_time,
        "emitted": emitted,
        "samples": samples,
        "electrically_verified": False,
    }
    dp.DPxClearError()
    if reply["error_code"] != "DPX_SUCCESS" or not ready:
        reply["ok"] = False
        reply["outcome"] = "device_error"
    elif config.simulate and not emitted:
        reply["ok"] = False
        reply["outcome"] = "no_emission"
    elif not config.simulate:
        if flush:
            reply["ok"] = True
            reply["outcome"] = "flushed_unverified"
        else:
            reply["ok"] = False
            reply["outcome"] = "uncommitted"
    else:
        reply["ok"] = True
        reply["outcome"] = "sent"
    return reply


def _worker_main(connection: Connection, config: WorkerConfig):
    dp = None
    close_device = False
    close_attempted = False
    try:
        dp = _backend(config)
        opened = _open_device(dp, config)
        connection.send(opened)
        if not opened["ok"]:
            return
        layout = opened["layout"]
        while True:
            command = connection.recv()
            if command["command"] == "trigger":
                connection.send(_trigger_device(dp, config, layout, command))
            elif command["command"] == "close":
                close_attempted = True
                try:
                    dp.DPxClose()
                except Exception as exc:
                    connection.send(
                        {"close_error": f"{type(exc).__name__}: {exc}"}
                    )
                    return
                connection.send({"ok": True, "outcome": "closed"})
                return
            else:
                raise WorkerProtocolError(f"unknown command: {command!r}")
    except (EOFError, BrokenPipeError):
        close_device = True
    except Exception as exc:
        close_device = True
        try:
            connection.send(
                {"protocol_error": f"{type(exc).__name__}: {exc}"}
            )
        except (BrokenPipeError, EOFError):
            pass
    finally:
        if close_device and not close_attempted and dp is not None:
            try:
                dp.DPxClose()
            except Exception:
                pass
        connection.close()


class DeviceWorker:
    def __init__(self, config: WorkerConfig):
        _validate_config(config)
        self.config = config
        self._process = None
        self._connection = None
        self._request_outstanding = False

    @property
    def is_alive(self):
        return self._process is not None and self._process.is_alive()

    def _receive(
        self,
        timeout,
        *,
        cancel_event=None,
        progress_callback=None,
    ):
        if self._connection is None:
            raise WorkerProtocolError("worker has not been started")
        deadline = (
            None if timeout is None else time.monotonic() + max(0.0, timeout)
        )
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise WorkerCancelled("worker reply cancelled")
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            poll_timeout = (
                _REPLY_POLL_SLICE
                if remaining is None
                else min(_REPLY_POLL_SLICE, remaining)
            )
            try:
                ready = self._connection.poll(poll_timeout)
            except (BrokenPipeError, EOFError, OSError) as exc:
                raise WorkerProtocolError("worker pipe poll failed") from exc
            if ready:
                break
            if progress_callback is not None:
                progress_callback()
            if cancel_event is not None and cancel_event.is_set():
                raise WorkerCancelled("worker reply cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                raise WorkerTimeout("worker reply timed out")
        try:
            reply = self._connection.recv()
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise WorkerProtocolError("worker pipe receive failed") from exc
        if not isinstance(reply, dict):
            raise WorkerProtocolError(f"invalid worker reply: {reply!r}")
        if "protocol_error" in reply:
            raise WorkerProtocolError(reply["protocol_error"])
        if "close_error" in reply:
            raise WorkerCloseError(reply["close_error"])
        return reply

    def _send(self, command):
        if self._connection is None:
            raise WorkerProtocolError("worker has not been started")
        try:
            self._connection.send(command)
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise WorkerProtocolError("worker pipe send failed") from exc

    def _cleanup_resources(self, *extra_connections):
        connection = self._connection
        process = self._process
        self._connection = None
        self._process = None
        self._request_outstanding = False

        seen = set()
        for candidate in (connection, *extra_connections):
            if candidate is None or id(candidate) in seen:
                continue
            seen.add(id(candidate))
            try:
                candidate.close()
            except OSError:
                pass
        if process is not None:
            process.close()

    def start(
        self,
        timeout,
        *,
        cancel_event=None,
        progress_callback=None,
    ):
        if self._process is not None:
            raise WorkerProtocolError("worker has already been started")
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        self._connection = parent
        try:
            self._process = context.Process(
                target=_worker_main,
                args=(child, self.config),
            )
            self._process.start()
        except BaseException:
            self._cleanup_resources(child)
            raise
        child.close()
        self._request_outstanding = True
        try:
            reply = self._receive(
                timeout,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
            )
        except BaseException:
            raise
        else:
            self._request_outstanding = False
            return reply

    def trigger(
        self,
        code,
        *,
        flush=True,
        timeout=None,
        cancel_event=None,
        progress_callback=None,
    ):
        if self._connection is None or not self.is_alive:
            raise WorkerProtocolError("worker is not running")
        if self._request_outstanding:
            raise WorkerProtocolError("worker request is already outstanding")
        max_code = (1 << self.config.num_bits) - 1
        if (
            not isinstance(code, int)
            or isinstance(code, bool)
            or not 0 <= code <= max_code
        ):
            raise ValueError(f"code must be between 0 and {max_code}")
        self._request_outstanding = True
        try:
            self._send(
                {"command": "trigger", "code": code, "flush": flush}
            )
            reply = self._receive(
                timeout,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
            )
        except BaseException:
            raise
        else:
            self._request_outstanding = False
            return reply

    def _wait_for_exit(
        self,
        deadline,
        *,
        cancel_event=None,
        progress_callback=None,
    ):
        while self.is_alive:
            if cancel_event is not None and cancel_event.is_set():
                raise WorkerCancelled("worker close cancelled")
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if remaining == 0:
                raise WorkerForcedTermination(
                    "worker close required forced termination"
                )
            join_timeout = (
                _REPLY_POLL_SLICE
                if remaining is None
                else min(_REPLY_POLL_SLICE, remaining)
            )
            self._process.join(join_timeout)
            if self.is_alive and progress_callback is not None:
                progress_callback()

    def close(
        self,
        timeout=1.0,
        *,
        cancel_event=None,
        progress_callback=None,
    ):
        if self._process is None:
            return
        if self._request_outstanding:
            self.terminate()
            raise WorkerForcedTermination(
                "outstanding worker request required forced termination"
            )
        deadline = (
            None if timeout is None else time.monotonic() + max(0.0, timeout)
        )
        try:
            if self.is_alive and self._connection is not None:
                self._send({"command": "close"})
                receive_timeout = (
                    None
                    if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                reply = self._receive(
                    receive_timeout,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                )
                if reply.get("ok") is not True or reply.get("outcome") != "closed":
                    raise WorkerCloseError(
                        f"invalid worker close reply: {reply!r}"
                    )
                self._wait_for_exit(
                    deadline,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                )
            else:
                self._process.join(0)
                raise WorkerCloseError(
                    "worker exited before device close could complete"
                )
        except WorkerTimeout as exc:
            self.terminate()
            raise WorkerTimeout("worker close timed out") from exc
        except BaseException:
            if self._process is not None:
                self.terminate()
            raise
        self._cleanup_resources()
        return reply

    def terminate(self):
        if self._process is not None:
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(1.0)
                if self._process.is_alive():
                    self._process.kill()
                    self._process.join(1.0)
            else:
                self._process.join(0)
        self._cleanup_resources()
