import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def versioning(monkeypatch):
    backend = Path(__file__).resolve().parents[1] / "build_backend"
    monkeypatch.syspath_prepend(str(backend))
    return importlib.import_module("versioning")


def git(root, *args, date=None):
    env = dict(os.environ)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.check_output(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=root,
        env=env,
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


def commit(root, date):
    git(root, "commit", "--allow-empty", "-m", "change", date=date)
    return git(root, "rev-parse", "HEAD")


def set_baseline(root, sha, major=1):
    (root / "version.json").write_text(json.dumps({"major": major, "commit": sha}))


def test_work_days_daily_commits_and_major_reset(tmp_path, versioning):
    git(tmp_path, "init", "-b", "main")
    commit(tmp_path, "2026-08-22T00:00:00-04:00")
    baseline = commit(tmp_path, "2026-08-22T01:00:00-04:00")
    set_baseline(tmp_path, baseline)
    assert versioning.git_version(tmp_path) == "1.0.1"
    commit(tmp_path, "2026-08-22T23:30:00-04:00")
    assert versioning.git_version(tmp_path) == "1.0.2"
    commit(tmp_path, "2026-09-30T01:00:00-04:00")
    assert versioning.git_version(tmp_path) == "1.1.1"
    latest = commit(tmp_path, "2026-09-30T02:00:00-04:00")
    assert versioning.git_version(tmp_path) == "1.1.2"
    set_baseline(tmp_path, latest, major=2)
    assert versioning.git_version(tmp_path) == "2.0.1"


def test_merge_counts_once_without_importing_branch_work_days(tmp_path, versioning):
    git(tmp_path, "init", "-b", "main")
    set_baseline(tmp_path, commit(tmp_path, "2026-08-22T01:00:00-04:00"))
    git(tmp_path, "checkout", "-b", "feature")
    commit(tmp_path, "2026-08-23T01:00:00-04:00")
    commit(tmp_path, "2026-08-24T01:00:00-04:00")
    git(tmp_path, "checkout", "main")
    git(
        tmp_path,
        "merge",
        "--no-ff",
        "feature",
        "-m",
        "merge",
        date="2026-09-30T01:00:00-04:00",
    )
    assert versioning.git_version(tmp_path) == "1.1.1"


def test_incomplete_history_and_backdated_days_are_rejected(tmp_path, versioning):
    git(tmp_path, "init", "-b", "main")
    baseline = commit(tmp_path, "2026-08-22T01:00:00-04:00")
    set_baseline(tmp_path, "0" * 40)
    with pytest.raises(ValueError, match="baseline is missing"):
        versioning.git_version(tmp_path)
    set_baseline(tmp_path, baseline)
    commit(tmp_path, "2026-08-21T01:00:00-04:00")
    with pytest.raises(ValueError, match="dates go backwards"):
        versioning.git_version(tmp_path)


def test_source_archive_uses_its_frozen_version(tmp_path, versioning):
    with pytest.raises(ValueError, match="Git checkout or a built source archive"):
        versioning.release_version(tmp_path)
    (tmp_path / "PKG-INFO").write_text("Name: qubotu\nVersion: 2.17.3\n")
    (tmp_path / "src/qubotu").mkdir(parents=True)
    assert versioning.write_version(tmp_path) == "2.17.3"
    assert '__version__ = "2.17.3"' in (tmp_path / "src/qubotu/_version.py").read_text()
    (tmp_path / "PKG-INFO").write_text("Version: 0.9.30.5\n")
    with pytest.raises(ValueError, match="invalid release version"):
        versioning.release_version(tmp_path)


def test_major_zero_is_the_pre_release_series(tmp_path, versioning):
    git(tmp_path, "init", "-b", "main")
    set_baseline(tmp_path, commit(tmp_path, "2026-06-15T03:35:00-04:00"), major=0)
    commit(tmp_path, "2026-06-15T03:40:00-04:00")
    commit(tmp_path, "2026-10-06T01:00:00-04:00")
    assert versioning.git_version(tmp_path) == "0.1.1"
    set_baseline(tmp_path, "a" * 40, major=-1)
    with pytest.raises(ValueError, match="major of 0 or more"):
        versioning.git_version(tmp_path)
    # A source archive from the pre-release series keeps its version.
    (tmp_path / "PKG-INFO").write_text("Version: 0.5.4\n")
    (tmp_path / ".git").rename(tmp_path / "git-moved")
    assert versioning.release_version(tmp_path) == "0.5.4"


def test_app_version_follows_new_commits_without_reinstalling(tmp_path, versioning):
    from qubotu.version import current_version

    git(tmp_path, "init", "-b", "main")
    set_baseline(tmp_path, commit(tmp_path, "2026-06-15T03:00:00-04:00"), major=0)
    assert current_version(tmp_path) == "0.0.1"
    commit(tmp_path, "2026-06-16T03:00:00-04:00")
    assert current_version(tmp_path) == "0.1.1"  # picked up, no reinstall
    commit(tmp_path, "2026-06-16T04:00:00-04:00")
    assert current_version(tmp_path) == "0.1.2"


def test_app_version_without_git_is_the_built_one(tmp_path):
    from qubotu._version import __version__
    from qubotu.version import current_version

    assert current_version(tmp_path) == __version__
