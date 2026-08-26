import csv
from dataclasses import asdict
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from harness.csv_log import CsvEventLog
from harness import provoke
from harness.provoke import (
    bandwidth_reader,
    build_soak_config,
    build_parser,
    main,
    prompt_and_mark,
    run_stale_handle_probe,
)


def csv_rows(path):
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def marker_names(path):
    return [
        row["marker"]
        for row in csv_rows(path)
        if row["row_type"] == "marker"
    ]


def test_exactly_one_provocation_is_required():
    """Would catch accepting either no intervention or confounded modes."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["--kvm", "--power"])
    assert parser.parse_args(["--kvm"]).kvm is True


def test_kvm_is_the_first_mode_shown_in_cli_help():
    """Would catch burying the recommended first-line intervention."""
    usage = build_parser().format_help()
    assert usage.index("--kvm") < usage.index("--power")


def test_bandwidth_cli_requires_a_source_file(tmp_path):
    """Would catch starting bandwidth mode without explicit read-only input."""
    with pytest.raises(SystemExit):
        main(
            [
                "--bandwidth",
                "--simulate",
                "--max-triggers",
                "0",
                "--csv",
                str(tmp_path / "missing.csv"),
            ]
        )


def test_kvm_prompt_marks_before_and_after_action(tmp_path):
    """Would catch a KVM action with no timestamped before/after boundary."""
    answers = iter(["", ""])
    path = tmp_path / "run.csv"
    with CsvEventLog(path) as log:
        prompt_and_mark(
            log,
            lambda _: next(answers),
            "kvm_switch",
            "Switch the KVM once, then return to this terminal.",
        )
    assert marker_names(path) == [
        "kvm_switch_requested",
        "kvm_switch_completed",
    ]


def test_bandwidth_reader_rewinds_existing_file_and_marks_bytes(tmp_path):
    """Would catch writing traffic or stopping instead of rewinding at EOF."""

    class StopAfterReads:
        def __init__(self, allowed_reads):
            self.allowed_reads = allowed_reads
            self.checks = 0

        def is_set(self):
            self.checks += 1
            return self.checks > self.allowed_reads

    source = tmp_path / "traffic.bin"
    source.write_bytes(b"abcdefgh")
    stop = StopAfterReads(3)
    path = tmp_path / "run.csv"
    with CsvEventLog(path) as log:
        total = bandwidth_reader(source, stop, log, chunk_size=4)
    assert total == 12
    assert source.read_bytes() == b"abcdefgh"
    assert marker_names(path) == ["bandwidth_started", "bandwidth_stopped"]


def test_bandwidth_reader_marks_each_gib_milestone(monkeypatch, tmp_path):
    """Would catch losing progress boundaries during sustained reads."""

    class StopAfterReads:
        def __init__(self):
            self.checks = 0

        def is_set(self):
            self.checks += 1
            return self.checks > 3

    monkeypatch.setattr(provoke, "_GIB", 4)
    source = tmp_path / "traffic.bin"
    source.write_bytes(b"abcdefgh")
    path = tmp_path / "run.csv"
    with CsvEventLog(path) as log:
        bandwidth_reader(source, StopAfterReads(), log, chunk_size=4)
    rows = csv_rows(path)
    milestones = [
        (row["marker"], row["detail"])
        for row in rows
        if row["marker"] == "bandwidth_gib_milestone"
    ]
    assert milestones == [
        ("bandwidth_gib_milestone", "1 GiB read"),
        ("bandwidth_gib_milestone", "2 GiB read"),
        ("bandwidth_gib_milestone", "3 GiB read"),
    ]


def test_bandwidth_reader_rejects_non_file_source(tmp_path):
    """Would catch accepting a directory as supposedly read-only traffic."""
    with CsvEventLog(tmp_path / "run.csv") as log:
        with pytest.raises(ValueError, match="regular readable file"):
            bandwidth_reader(tmp_path, object(), log)


def test_stale_handle_probe_records_cross_process_reopen_refusal(tmp_path):
    """Would catch opening and closing in-process instead of leaking a child handle."""
    marker = tmp_path / "mock-handle"
    path = tmp_path / "run.csv"
    with CsvEventLog(path) as event_log:
        result = run_stale_handle_probe(
            simulate=True,
            persistent_handle_path=marker,
            event_log=event_log,
            timeout=1.0,
        )
    assert result["ok"] is False
    assert result["error_code"] == "DPX_ERR_ALREADY_OPEN"
    assert result["opener_pid"] != os.getpid()
    assert result["child_exitcode"] == 0
    assert marker.exists()
    assert marker_names(path) == [
        "stale_handle_opened",
        "stale_handle_reopen_refused",
    ]


def test_no_flush_changes_only_the_flush_configuration():
    """Would catch no-flush silently changing timing or device parameters."""
    parser = build_parser()
    baseline = asdict(build_soak_config(parser.parse_args(["--kvm"])))
    no_flush = asdict(build_soak_config(parser.parse_args(["--no-flush"])))
    changed = {
        key: (baseline[key], no_flush[key])
        for key in baseline
        if baseline[key] != no_flush[key]
    }
    assert changed == {"flush_triggers": (True, False)}


@pytest.mark.parametrize(
    "mode",
    ["--power", "--bandwidth", "--stale-handle", "--wiggle"],
)
def test_other_modes_do_not_change_soak_configuration(mode):
    """Would catch a provocation confounding the baseline soak settings."""
    parser = build_parser()
    baseline = build_soak_config(parser.parse_args(["--kvm"]))
    assert build_soak_config(parser.parse_args([mode])) == baseline


def test_wiggle_prompts_follow_fixed_connector_order(tmp_path):
    """Would catch combining or reordering cable interventions."""
    csv_path = tmp_path / "wiggle.csv"
    prompts = []
    assert main(
        [
            "--wiggle",
            "--simulate",
            "--max-triggers",
            "0",
            "--csv",
            str(csv_path),
            "--text-log",
            str(tmp_path / "wiggle.log"),
        ],
        input_fn=lambda prompt: prompts.append(prompt) or "",
    ) == 0
    assert marker_names(csv_path)[:6] == [
        "wiggle_mac_connector_requested",
        "wiggle_mac_connector_completed",
        "wiggle_datapixx_connector_requested",
        "wiggle_datapixx_connector_completed",
        "wiggle_msr_feedthrough_requested",
        "wiggle_msr_feedthrough_completed",
    ]
    instructions = [prompts[index] for index in (0, 2, 4)]
    assert "Mac-side USB connector" in instructions[0]
    assert "DATAPixx-side USB connector" in instructions[1]
    assert "MSR feedthrough connector" in instructions[2]


def test_kvm_markers_and_soak_rows_share_one_csv(tmp_path):
    """Would catch splitting provocation timestamps from the soak timeline."""
    csv_path = tmp_path / "shared.csv"
    assert main(
        [
            "--kvm",
            "--simulate",
            "--interval",
            "0",
            "--max-triggers",
            "1",
            "--csv",
            str(csv_path),
            "--text-log",
            str(tmp_path / "shared.log"),
        ],
        input_fn=lambda _: "",
    ) == 0
    rows = csv_rows(csv_path)
    assert {row["row_type"] for row in rows} == {"marker", "trigger"}
    assert "kvm_switch_requested" in marker_names(csv_path)
    assert [row["outcome"] for row in rows if row["row_type"] == "trigger"] == [
        "sent"
    ]


def test_no_flush_integration_records_no_emission(tmp_path):
    """Would catch the no-flush mode accidentally committing the trigger."""
    csv_path = tmp_path / "no-flush.csv"
    assert main(
        [
            "--no-flush",
            "--simulate",
            "--interval",
            "0",
            "--reopen-interval",
            "0",
            "--max-triggers",
            "1",
            "--csv",
            str(csv_path),
            "--text-log",
            str(tmp_path / "no-flush.log"),
        ]
    ) == 0
    outcomes = [
        row["outcome"]
        for row in csv_rows(csv_path)
        if row["row_type"] == "trigger"
    ]
    assert outcomes == ["no_emission"]


def test_main_restores_sigint_handler_and_joins_soak_thread(tmp_path):
    """Would catch leaking global signal state or a background soak thread."""
    original = signal.getsignal(signal.SIGINT)

    def sentinel_handler(signum, frame):
        raise AssertionError("restored sentinel should not run")

    signal.signal(signal.SIGINT, sentinel_handler)
    try:
        assert main(
            [
                "--no-flush",
                "--simulate",
                "--max-triggers",
                "0",
                "--csv",
                str(tmp_path / "cleanup.csv"),
                "--text-log",
                str(tmp_path / "cleanup.log"),
            ]
        ) == 0
        assert signal.getsignal(signal.SIGINT) is sentinel_handler
        assert all(
            thread.name != "dpx-provocation-soak"
            for thread in threading.enumerate()
        )
    finally:
        signal.signal(signal.SIGINT, original)


@pytest.mark.parametrize(
    ("mode_args", "stdin", "expected_marker"),
    [
        (["--kvm"], "\n\n", "kvm_switch_completed"),
        (["--power"], "\n\n", "power_cycle_completed"),
        (["--bandwidth"], "", "bandwidth_stopped"),
        (["--stale-handle"], "", "stale_handle_reopen_refused"),
        (["--no-flush"], "", "worker_closed"),
        (["--wiggle"], "\n\n\n\n\n\n", "wiggle_msr_feedthrough_completed"),
    ],
)
def test_short_simulated_cli_path(mode_args, stdin, expected_marker, tmp_path):
    """Would catch a mode that cannot complete through the actual module CLI."""
    case = mode_args[0].removeprefix("--")
    csv_path = tmp_path / f"{case}.csv"
    args = [
        sys.executable,
        "-m",
        "harness.provoke",
        *mode_args,
        "--simulate",
        "--interval",
        "0",
        "--reopen-interval",
        "0.01",
        "--csv",
        str(csv_path),
        "--text-log",
        str(tmp_path / f"{case}.log"),
    ]
    if mode_args == ["--stale-handle"]:
        args.extend(
            [
                "--duration",
                "0.05",
                "--persistent-handle-path",
                str(tmp_path / "stale-marker"),
            ]
        )
    else:
        args.extend(["--max-triggers", "1"])
    if mode_args == ["--bandwidth"]:
        source = tmp_path / "source.bin"
        source.write_bytes(b"read-only traffic")
        args.extend(["--bandwidth-source", str(source)])

    completed = subprocess.run(
        args,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert expected_marker in marker_names(csv_path)


def test_sigint_stops_cli_and_closes_worker(tmp_path):
    """Would catch Ctrl-C leaving the worker or orchestration thread alive."""
    csv_path = tmp_path / "interrupt.csv"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "harness.provoke",
            "--no-flush",
            "--simulate",
            "--interval",
            "0.01",
            "--reopen-interval",
            "0.01",
            "--csv",
            str(csv_path),
            "--text-log",
            str(tmp_path / "interrupt.log"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not csv_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    process.send_signal(signal.SIGINT)
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, (stdout, stderr)
    assert "worker_closed" in marker_names(csv_path)
