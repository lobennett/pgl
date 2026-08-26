"""Controlled, independently selectable USB failure provocations."""

from __future__ import annotations

import argparse
from dataclasses import replace
import multiprocessing
import os
from pathlib import Path
import signal
import threading

from harness.csv_log import CsvEventLog
from harness.dpx_worker import DeviceWorker, WorkerConfig, WorkerTimeout
from harness.soak import SoakConfig, SoakRunner


_GIB = 1024**3


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run one controlled provocation during a DATAPixx soak."
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--kvm", action="store_true")
    modes.add_argument("--power", action="store_true")
    modes.add_argument("--bandwidth", action="store_true")
    modes.add_argument("--stale-handle", action="store_true")
    modes.add_argument("--no-flush", action="store_true")
    modes.add_argument("--wiggle", action="store_true")
    parser.add_argument("--bandwidth-source")
    parser.add_argument(
        "--persistent-handle-path",
        default="dpx-stale-handle",
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
    return parser


def build_soak_config(args):
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


def prompt_and_mark(log, input_fn, marker_prefix, instruction):
    log.write_marker(f"{marker_prefix}_requested", instruction)
    input_fn(f"{instruction}\nPress Enter when ready to perform it: ")
    input_fn("Press Enter after completing the action: ")
    log.write_marker(f"{marker_prefix}_completed", instruction)


def bandwidth_reader(path, stop_event, log, chunk_size=8 * 1024 * 1024):
    source = Path(path)
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    if not source.is_file():
        raise ValueError("bandwidth source must be a regular readable file")

    total = 0
    next_milestone = _GIB
    try:
        stream = source.open("rb")
    except OSError as exc:
        raise ValueError(
            "bandwidth source must be a regular readable file"
        ) from exc

    with stream:
        if source.stat().st_size == 0:
            raise ValueError(
                "bandwidth source must be a non-empty regular readable file"
            )
        log.write_marker("bandwidth_started", str(source))
        try:
            while not stop_event.is_set():
                block = stream.read(chunk_size)
                if not block:
                    stream.seek(0)
                    block = stream.read(chunk_size)
                total += len(block)
                while total >= next_milestone:
                    milestone = next_milestone // _GIB
                    log.write_marker(
                        "bandwidth_gib_milestone",
                        f"{milestone} GiB read",
                    )
                    next_milestone += _GIB
        finally:
            log.write_marker("bandwidth_stopped", f"{total} bytes read")
    return total


def _stale_handle_child(connection, simulate, persistent_handle_path):
    exit_code = 1
    try:
        if simulate:
            from harness import mock_dpx as backend

            backend.configure(
                persistent_handle_path=str(persistent_handle_path)
            )
        else:
            from pypixxlib import _libdpx as backend

        backend.DPxOpen()
        ready = backend.DPxIsReady()
        error_code = backend.DPxGetError()
        error_string = backend.DPxGetErrorString()
        connection.send(
            {
                "ok": ready and error_code == "DPX_SUCCESS",
                "ready": ready,
                "error_code": error_code,
                "error_string": error_string,
                "opener_pid": os.getpid(),
            }
        )
        exit_code = 0
    except BaseException as exc:
        try:
            connection.send(
                {
                    "ok": False,
                    "ready": False,
                    "error_code": type(exc).__name__,
                    "error_string": str(exc),
                    "opener_pid": os.getpid(),
                }
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()
        os._exit(exit_code)


def run_stale_handle_probe(
    *, simulate, persistent_handle_path, event_log, timeout
):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=True)
    process = context.Process(
        target=_stale_handle_child,
        args=(child, simulate, persistent_handle_path),
    )
    try:
        process.start()
        child.close()
        if not parent.poll(timeout):
            raise WorkerTimeout("stale-handle child open timed out")
        opened = parent.recv()
        process.join(timeout)
        if process.is_alive():
            raise WorkerTimeout("stale-handle child exit timed out")
        opened["child_exitcode"] = process.exitcode
    finally:
        if process.is_alive():
            process.terminate()
            process.join(1.0)
            if process.is_alive():
                process.kill()
                process.join(1.0)
        parent.close()
        child.close()
        process.close()

    if not opened["ok"]:
        event_log.write_marker(
            "stale_handle_open_failed",
            opened.get("error_string", ""),
            error_code=opened.get("error_code", ""),
            error_string=opened.get("error_string", ""),
            ready=opened.get("ready", False),
            outcome="open_failed",
        )
        return opened

    event_log.write_marker(
        "stale_handle_opened",
        f"child_pid={opened['opener_pid']}",
        error_code=opened["error_code"],
        error_string=opened["error_string"],
        ready=opened["ready"],
        outcome="opened_without_close",
    )
    simulation = (
        {"persistent_handle_path": str(persistent_handle_path)}
        if simulate
        else {}
    )
    worker = DeviceWorker(
        WorkerConfig(simulate=simulate, simulation=simulation)
    )
    try:
        result = worker.start(timeout=timeout)
    finally:
        worker.terminate()
    result.update(
        opener_pid=opened["opener_pid"],
        child_exitcode=opened["child_exitcode"],
    )
    marker = (
        "stale_handle_reopened"
        if result.get("ok")
        else "stale_handle_reopen_refused"
    )
    event_log.write_marker(
        marker,
        result.get("error_string", ""),
        error_code=result.get("error_code", ""),
        error_string=result.get("error_string", ""),
        ready=result.get("ready", False),
        outcome="opened" if result.get("ok") else "open_failed",
    )
    return result


def _run_prompted_mode(args, event_log, input_fn):
    if args.kvm:
        prompt_and_mark(
            event_log,
            input_fn,
            "kvm_switch",
            "Switch the KVM once, then return to this terminal.",
        )
    elif args.power:
        prompt_and_mark(
            event_log,
            input_fn,
            "power_cycle",
            "Power-cycle only the DATAPixx once.",
        )
    elif args.wiggle:
        actions = (
            (
                "wiggle_mac_connector",
                "Wiggle only the Mac-side USB connector once.",
            ),
            (
                "wiggle_datapixx_connector",
                "Wiggle only the DATAPixx-side USB connector once.",
            ),
            (
                "wiggle_msr_feedthrough",
                "Wiggle only the MSR feedthrough connector once.",
            ),
        )
        for marker_prefix, instruction in actions:
            prompt_and_mark(
                event_log,
                input_fn,
                marker_prefix,
                instruction,
            )


def _worker_factory(args):
    if not (args.stale_handle and args.simulate):
        return DeviceWorker
    persistent_handle_path = str(args.persistent_handle_path)

    def create_worker(config):
        simulation = dict(config.simulation)
        simulation["persistent_handle_path"] = persistent_handle_path
        return DeviceWorker(replace(config, simulation=simulation))

    return create_worker


def _join(thread):
    while thread.is_alive():
        thread.join(0.1)


def main(argv=None, *, input_fn=input):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.bandwidth and args.bandwidth_source is None:
        parser.error("--bandwidth requires --bandwidth-source")

    event_log = CsvEventLog(args.csv)
    runner = SoakRunner(
        build_soak_config(args),
        event_log,
        worker_factory=_worker_factory(args),
    )
    provocation_stop = threading.Event()
    thread_errors = []
    soak_thread = None
    previous_handler = None
    marker_path = Path(args.persistent_handle_path)
    remove_marker = args.stale_handle and args.simulate and not marker_path.exists()

    def request_stop(_signum, _frame):
        runner.stop_event.set()
        provocation_stop.set()
        raise KeyboardInterrupt

    def run_soak():
        try:
            runner.run()
        except BaseException as exc:
            thread_errors.append(exc)
        finally:
            provocation_stop.set()

    try:
        previous_handler = signal.signal(signal.SIGINT, request_stop)
        if args.stale_handle:
            run_stale_handle_probe(
                simulate=args.simulate,
                persistent_handle_path=marker_path,
                event_log=event_log,
                timeout=args.call_timeout,
            )

        soak_thread = threading.Thread(
            target=run_soak,
            name="dpx-provocation-soak",
        )
        soak_thread.start()
        if args.kvm or args.power or args.wiggle:
            _run_prompted_mode(args, event_log, input_fn)
        elif args.bandwidth:
            bandwidth_reader(
                args.bandwidth_source,
                provocation_stop,
                event_log,
            )
        _join(soak_thread)
        if thread_errors:
            raise thread_errors[0]
    except KeyboardInterrupt:
        runner.stop_event.set()
        provocation_stop.set()
    finally:
        runner.stop_event.set()
        provocation_stop.set()
        if soak_thread is not None:
            _join(soak_thread)
        event_log.close()
        if previous_handler is not None:
            signal.signal(signal.SIGINT, previous_handler)
        if remove_marker:
            try:
                marker_path.unlink()
            except FileNotFoundError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
