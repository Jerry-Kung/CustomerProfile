#!/usr/bin/env python3
"""Lint, render, and build a self-contained private Helm package."""

from __future__ import annotations

import argparse
import copy
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import Any

import yaml


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = copy.deepcopy(base)
        for key, value in override.items():
            merged[key] = _deep_merge(merged.get(key), value)
        return merged
    return copy.deepcopy(override)


def _run(command: list[str], *, quiet: bool = False) -> None:
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL if quiet else None,
    )


def _assert_no_placeholders(value: Any, path: str = "values") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _assert_no_placeholders(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_placeholders(child, f"{path}[{index}]")
    elif isinstance(value, str) and (
        "REPLACE_WITH" in value or value.strip() == ""
    ):
        raise ValueError(f"private production value is unset: {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--chart",
        type=Path,
        default=Path("deploy/helm/customer-profile-backend"),
    )
    parser.add_argument(
        "--values",
        required=True,
        type=Path,
        action="append",
        help=(
            "Values file to merge into the package. Repeat the flag to layer "
            "private values and a workflow-target topology overlay."
        ),
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("dist/helm"),
    )
    parser.add_argument("--release-name", default="customer-profile-backend")
    parser.add_argument("--namespace", default="aizt01-pro")
    parser.add_argument("--version", help="Override Chart.yaml version")
    args = parser.parse_args()

    base_values = yaml.safe_load((args.chart / "values.yaml").read_text())
    merged_values = base_values
    for values_path in args.values:
        override_values = yaml.safe_load(values_path.read_text())
        merged_values = _deep_merge(merged_values, override_values)
    _assert_no_placeholders(merged_values)

    values_args = [
        item
        for values_path in args.values
        for item in ("-f", str(values_path))
    ]
    _run(["helm", "lint", str(args.chart), *values_args])
    _run(
        [
            "helm",
            "template",
            args.release_name,
            str(args.chart),
            "--namespace",
            args.namespace,
            *values_args,
        ],
        quiet=True,
    )

    args.destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="customer-profile-backend-chart-") as temp:
        staged_chart = Path(temp) / args.chart.name
        shutil.copytree(args.chart, staged_chart)
        for private_path in staged_chart.glob("values*.private.yaml"):
            private_path.unlink()
        (staged_chart / "values.yaml").write_text(
            yaml.safe_dump(merged_values, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        package_command = [
            "helm",
            "package",
            str(staged_chart),
            "--destination",
            str(args.destination),
            "--app-version",
            str(merged_values["image"]["tag"]),
        ]
        if args.version:
            package_command.extend(["--version", args.version])
        before = set(args.destination.glob("*.tgz"))
        _run(package_command)
        created = set(args.destination.glob("*.tgz")) - before
        if not created:
            candidates = sorted(
                args.destination.glob("*.tgz"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if not candidates:
                raise RuntimeError("helm package did not produce a .tgz file")
            package_path = candidates[0]
        else:
            package_path = max(created, key=lambda path: path.stat().st_mtime)

    package_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print(
        f"private Helm package created: {package_path} "
        "(mode 0600; package contains production secrets)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
