"""Inspect macOS USB topology and report possible bus-power overdraw."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Any


@dataclass(frozen=True)
class UsbNode:
    """One USB device or hub, retained in depth-first topology order."""

    name: str
    depth: int
    available_current_ma: int | None
    required_current_ma: int | None
    parent_available_current_ma: int | None
    vendor_id: str | None
    product_id: str | None
    location_id: str | None
    is_over_current: bool


def parse_current_ma(value: object) -> int | None:
    """Return a current value in milliamps, if *value* is recognizable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([+-]?[0-9][0-9,]*)\s*(?:mA)?", value.strip())
    if match is None:
        return None
    return int(match.group(1).replace(",", ""))


def _normalized_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _item_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item:
            return item[key]

    normalized_keys = {_normalized_key(key) for key in keys}
    for key, value in item.items():
        if _normalized_key(key) in normalized_keys:
            return value
    return None


def _current_value(item: dict[str, Any], key: str) -> int | None:
    return parse_current_ma(
        _item_value(item, key, f"{key} (mA)", key.replace("_", " "))
    )


def walk_usb_tree(profile: dict[str, Any]) -> list[UsbNode]:
    """Flatten a system_profiler USB profile while preserving each node's depth."""
    nodes: list[UsbNode] = []
    roots = profile.get("SPUSBDataType", [])
    if not isinstance(roots, list):
        return nodes

    def visit(item: object, depth: int, parent_limit: int | None) -> None:
        if not isinstance(item, dict):
            return
        available = _current_value(item, "current_available")
        required = _current_value(item, "current_required")
        nodes.append(
            UsbNode(
                name=str(_item_value(item, "_name", "name") or "Unnamed USB device"),
                depth=depth,
                available_current_ma=available,
                required_current_ma=required,
                parent_available_current_ma=parent_limit,
                vendor_id=_optional_text(_item_value(item, "vendor_id", "Vendor ID")),
                product_id=_optional_text(_item_value(item, "product_id", "Product ID")),
                location_id=_optional_text(_item_value(item, "location_id", "Location ID")),
                is_over_current=(
                    required is not None
                    and parent_limit is not None
                    and required > parent_limit
                ),
            )
        )
        child_limit = available if available is not None else parent_limit
        children = _item_value(item, "_items", "items")
        if isinstance(children, list):
            for child in children:
                visit(child, depth + 1, child_limit)

    for root in roots:
        visit(root, 0, None)
    return nodes


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def format_usb_tree(nodes: list[UsbNode]) -> str:
    """Render a readable, indented USB hierarchy with current-limit warnings."""
    lines: list[str] = []
    for node in nodes:
        details: list[str] = []
        for label, value in (
            ("vendor_id", node.vendor_id),
            ("product_id", node.product_id),
            ("location_id", node.location_id),
        ):
            if value is not None:
                details.append(f"{label}={value}")
        if node.available_current_ma is not None:
            details.append(f"available={node.available_current_ma} mA")
        if node.required_current_ma is not None:
            details.append(f"required={node.required_current_ma} mA")
        if node.parent_available_current_ma is not None:
            details.append(f"parent limit={node.parent_available_current_ma} mA")

        indent = "  " * node.depth
        suffix = f" [{'; '.join(details)}]" if details else ""
        lines.append(f"{indent}{node.name}{suffix}")
        if node.is_over_current:
            lines.append(
                f"{indent}OVER CURRENT: requires {node.required_current_ma} mA, "
                f"parent offers {node.parent_available_current_ma} mA"
            )
    return "\n".join(lines)


def collect_usb_profile() -> dict[str, Any]:
    """Collect and decode the USB section of macOS system_profiler output."""
    try:
        completed = subprocess.run(
            ["system_profiler", "SPUSBDataType", "-json"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("system_profiler is unavailable") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"system_profiler failed (exit {exc.returncode})") from exc
    return _parse_profile_json(completed.stdout, "system_profiler output")


def _parse_profile_json(raw_json: str, source: str) -> dict[str, Any]:
    try:
        profile = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{source} contains invalid JSON") from exc
    if not isinstance(profile, dict):
        raise RuntimeError(f"{source} must contain a JSON object")
    return profile


def _load_input_profile(path: Path) -> dict[str, Any]:
    try:
        return _parse_profile_json(path.read_text(), f"input JSON {path}")
    except OSError as exc:
        raise RuntimeError(f"cannot read input JSON {path}: {exc.strerror}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report USB topology and possible bus-power overdraw."
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="write the unmodified parsed USB profile as indented JSON",
    )
    parser.add_argument(
        "--input-json",
        type=Path,
        help="parse an existing system_profiler JSON file without macOS tools",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        profile = (
            _load_input_profile(args.input_json)
            if args.input_json is not None
            else collect_usb_profile()
        )
        if args.json_output is not None:
            args.json_output.write_text(json.dumps(profile, indent=2) + "\n")
        print(format_usb_tree(walk_usb_tree(profile)))
    except RuntimeError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
