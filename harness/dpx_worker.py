"""Run DATAPixx calls behind a persistent, spawn-process boundary."""

from __future__ import annotations

import multiprocessing
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from typing import Any


class WorkerTimeout(TimeoutError):
    """Raised when the device process does not reply before its deadline."""


class WorkerProtocolError(RuntimeError):
    """Raised when the device process sends an invalid reply."""


@dataclass(frozen=True)
class WorkerConfig:
    simulate: bool = False
    simulation: dict[str, Any] = field(default_factory=dict)
    num_bits: int = 8
    pulse_len: int = 3
    sampling_rate: int = 1000
    ram_base_address: int = 8_000_000


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
    emitted = (
        len(dp.get_mock_state()["emissions"]) > before
        if config.simulate
        else None
    )
    reply = {
        "ok": ready and error_code == "DPX_SUCCESS",
        "ready": ready,
        "error_code": error_code,
        "error_string": error_string,
        "device_time": device_time,
        "emitted": emitted,
    }
    dp.DPxClearError()
    if reply["error_code"] != "DPX_SUCCESS" or not ready:
        reply["ok"] = False
        reply["outcome"] = "device_error"
    elif config.simulate and not emitted:
        reply["ok"] = False
        reply["outcome"] = "no_emission"
    elif not config.simulate and not flush:
        reply["ok"] = False
        reply["outcome"] = "uncommitted"
    else:
        reply["ok"] = True
        reply["outcome"] = "sent"
    return reply


def _worker_main(connection: Connection, config: WorkerConfig):
    dp = None
    close_device = False
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
                close_device = True
                connection.send({"ok": True})
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
        if close_device and dp is not None:
            try:
                dp.DPxClose()
            except Exception:
                pass
        connection.close()


class DeviceWorker:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self._process = None
        self._connection = None

    @property
    def is_alive(self):
        return self._process is not None and self._process.is_alive()

    def _receive(self, timeout):
        if self._connection is None:
            raise WorkerProtocolError("worker has not been started")
        if not self._connection.poll(timeout):
            raise WorkerTimeout("worker reply timed out")
        try:
            reply = self._connection.recv()
        except (EOFError, OSError) as exc:
            raise WorkerProtocolError("worker exited without a reply") from exc
        if not isinstance(reply, dict):
            raise WorkerProtocolError(f"invalid worker reply: {reply!r}")
        if "protocol_error" in reply:
            raise WorkerProtocolError(reply["protocol_error"])
        return reply

    def start(self, timeout):
        if self._process is not None:
            raise WorkerProtocolError("worker has already been started")
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        self._connection = parent
        self._process = context.Process(target=_worker_main, args=(child, self.config))
        self._process.start()
        child.close()
        return self._receive(timeout)

    def trigger(self, code, *, flush=True, timeout=None):
        if self._connection is None or not self.is_alive:
            raise WorkerProtocolError("worker is not running")
        max_code = (1 << self.config.num_bits) - 1
        if (
            not isinstance(code, int)
            or isinstance(code, bool)
            or not 0 <= code <= max_code
        ):
            raise ValueError(f"code must be between 0 and {max_code}")
        self._connection.send(
            {"command": "trigger", "code": code, "flush": flush}
        )
        return self._receive(timeout)

    def close(self, timeout=1.0):
        if self._process is None:
            return
        if self.is_alive and self._connection is not None:
            try:
                self._connection.send({"command": "close"})
                self._receive(timeout)
                self._process.join(timeout)
            except (BrokenPipeError, EOFError, WorkerProtocolError, WorkerTimeout):
                self.terminate()
                return
            if self.is_alive:
                self.terminate()
                return
        else:
            self._process.join(0)
        if self._connection is not None:
            self._connection.close()
            self._connection = None

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
        if self._connection is not None:
            self._connection.close()
            self._connection = None
