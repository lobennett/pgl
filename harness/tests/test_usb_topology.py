import json
from pathlib import Path
import subprocess
import sys

import pytest

from harness import usb_topology
from harness.usb_topology import (
    collect_usb_profile,
    format_usb_tree,
    parse_current_ma,
    walk_usb_tree,
)


@pytest.fixture
def fixture_path():
    return Path("harness/tests/fixtures/system_profiler_usb.json")


@pytest.fixture
def fixture_profile(fixture_path):
    return json.loads(fixture_path.read_text())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("500 mA", 500),
        ("1,500 mA", 1500),
        (500, 500),
        (None, None),
        ("", None),
    ],
)
def test_parse_current_ma_normalizes_supported_values(value, expected):
    """Would catch dropping numeric, comma-separated, or absent currents."""
    assert parse_current_ma(value) == expected


def test_tree_preserves_hierarchy_and_flags_overdraw(fixture_profile):
    """Would catch flattening the USB tree or missing parent-limit overdraw."""
    rendered = format_usb_tree(walk_usb_tree(fixture_profile))
    lines = rendered.splitlines()
    assert lines[0].startswith("USB Controller")
    assert lines[1].startswith("  Hub")
    assert lines[2].startswith("    Camera")
    assert lines[3].startswith("    External SSD")
    assert "OVER CURRENT: requires 900 mA, parent offers 500 mA" in rendered


def test_walk_uses_nearest_parent_limit_and_display_style_keys():
    """Would catch a descendant inheriting a grandparent instead of its hub."""
    profile = {
        "SPUSBDataType": [
            {
                "_name": "Controller",
                "Current Available (mA)": "1,500",
                "_items": [
                    {
                        "_name": "Powered Hub",
                        "current_available": "700 mA",
                        "_items": [
                            {
                                "_name": "Keyboard",
                                "Current Required (mA)": 650,
                            },
                            {
                                "_name": "Drive",
                                "current_required": "900 mA",
                            },
                        ],
                    }
                ],
            }
        ]
    }

    nodes = walk_usb_tree(profile)

    assert [node.depth for node in nodes] == [0, 1, 2, 2]
    assert nodes[2].parent_available_current_ma == 700
    assert nodes[2].is_over_current is False
    assert nodes[3].parent_available_current_ma == 700
    assert nodes[3].is_over_current is True


def test_walk_handles_an_empty_profile():
    """Would catch assuming system_profiler always returns a controller."""
    assert walk_usb_tree({"SPUSBDataType": []}) == []


def test_collect_usb_profile_runs_the_exact_macos_command(monkeypatch):
    """Would catch changing the command or failing to parse its JSON output."""
    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args[0], 0, '{"SPUSBDataType": []}')

    monkeypatch.setattr(usb_topology.subprocess, "run", fake_run)

    assert collect_usb_profile() == {"SPUSBDataType": []}
    assert captured == {
        "args": (["system_profiler", "SPUSBDataType", "-json"],),
        "kwargs": {"check": True, "capture_output": True, "text": True},
    }


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (FileNotFoundError(), "system_profiler is unavailable"),
        (
            subprocess.CalledProcessError(1, ["system_profiler"], stderr="bad"),
            "system_profiler failed",
        ),
    ],
)
def test_collect_usb_profile_reports_subprocess_failures(monkeypatch, error, message):
    """Would catch leaking platform exceptions instead of a usable diagnostic."""
    def fake_run(*args, **kwargs):
        raise error

    monkeypatch.setattr(usb_topology.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match=message):
        collect_usb_profile()


def test_collect_usb_profile_reports_invalid_command_json(monkeypatch):
    """Would catch treating malformed system_profiler output as a USB tree."""
    monkeypatch.setattr(
        usb_topology.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "not json"),
    )

    with pytest.raises(RuntimeError, match="invalid JSON"):
        collect_usb_profile()


def test_cli_uses_input_json_writes_raw_profile_and_skips_system_profiler(
    fixture_path, tmp_path
):
    """Would catch offline parsing invoking macOS tools or altering raw JSON."""
    output_path = tmp_path / "raw-profile.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "harness.usb_topology",
            "--input-json",
            str(fixture_path),
            "--json-output",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "    Camera" in result.stdout
    assert "OVER CURRENT: requires 900 mA, parent offers 500 mA" in result.stdout
    assert json.loads(output_path.read_text()) == json.loads(fixture_path.read_text())


def test_cli_reports_invalid_input_json(tmp_path):
    """Would catch a malformed offline fixture producing a traceback."""
    input_path = tmp_path / "invalid.json"
    input_path.write_text("not json")
    result = subprocess.run(
        [sys.executable, "-m", "harness.usb_topology", "--input-json", str(input_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "invalid JSON" in result.stderr
