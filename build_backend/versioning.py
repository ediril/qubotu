"""Release identity from the selected major version's first-parent history."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from email.parser import Parser
from pathlib import Path

VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.[1-9][0-9]*")


def git_version(root: Path) -> str:
    baseline = json.loads((root / "version.json").read_text())
    major, commit = baseline["major"], baseline["commit"]
    # Major 0 is the pre-release series.
    if type(major) is not int or major < 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(
            "version.json requires a major of 0 or more and a full commit SHA"
        )
    result = subprocess.run(
        ["git", "log", "--first-parent", "--format=%H %cI", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    dates = []
    for line in result.stdout.splitlines():
        sha, timestamp = line.split()
        dates.append(datetime.fromisoformat(timestamp).date())
        if sha == commit:
            break
    else:
        raise ValueError(
            "major-version baseline is missing from first-parent history; "
            "fetch the full history or correct version.json"
        )
    if dates != sorted(dates, reverse=True):
        raise ValueError("commit dates go backwards in the major version's history")
    return f"{major}.{len(set(dates)) - 1}.{dates.count(dates[0])}"


def release_version(root: Path) -> str:
    if (root / ".git").exists():
        return git_version(root)

    # An sdist carries the version fixed when it was built, without needing Git.
    metadata = root / "PKG-INFO"
    if not metadata.is_file():
        raise ValueError("version requires a Git checkout or a built source archive")
    version = Parser().parsestr(metadata.read_text())["Version"]
    if version is None or VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError("source archive has an invalid release version")
    return version


def write_version(root: Path) -> str:
    version = release_version(root)
    path = root / "src" / "qubotu" / "_version.py"
    content = f'# Generated at build time; do not edit.\n__version__ = "{version}"\n'
    if not path.exists() or path.read_text() != content:
        path.write_text(content)
    return version
