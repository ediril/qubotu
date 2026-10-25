import os
import plistlib
import stat
import subprocess
import threading

import pytest

from qubotu import cli, config, version
from qubotu.db import DB


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setattr(config, "HOME", h)
    monkeypatch.setattr(config, "AGENTS_DIR", h / "agents")
    monkeypatch.setattr(config, "MODELS_DIR", h / "models")
    monkeypatch.setattr(config, "LOG_PATH", h / "daemon.log")
    config.ensure_dirs()
    return h


def test_only_one_daemon_per_data_folder(home):
    held = cli.acquire_lock()
    with pytest.raises(SystemExit, match=f"already running for {home}.*pid {os.getpid()}"):
        cli.acquire_lock()
    held.close()
    cli.acquire_lock().close()


def test_service_keeps_the_data_folder(home, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "PLIST", tmp_path / "agent.plist")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    cli.service_install(8765)
    plist = plistlib.loads((tmp_path / "agent.plist").read_bytes())
    assert plist["EnvironmentVariables"]["QUBOTU_HOME"] == str(home)
    assert plist["ProgramArguments"][-1] == "--wait", "the service waits instead of exiting into a restart loop"


def test_private_files(home):
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    db = DB(home / "q.db")
    db.set_setting("fireworks_api_key", "secret")
    assert stat.S_IMODE((home / "q.db").stat().st_mode) == 0o600


def test_broken_git_falls_back_to_built_version(tmp_path, monkeypatch):
    (tmp_path / "version.json").write_text("{}")
    (tmp_path / ".git" / "logs").mkdir(parents=True)
    (tmp_path / ".git" / "logs" / "HEAD").write_text("x")

    class Broken:
        @staticmethod
        def git_version(root):
            raise subprocess.CalledProcessError(128, ["git", "log"])

    monkeypatch.setattr(version, "_versioning", lambda: Broken)
    version._cache.pop("key", None)
    assert version.current_version(tmp_path) == version.built_version


def test_waiting_daemon_takes_over_when_the_other_stops(home, capsys):
    held = cli.acquire_lock()
    got = []
    waiter = threading.Thread(target=lambda: got.append(cli.acquire_lock(wait=True)))
    waiter.start()
    waiter.join(0.3)
    assert waiter.is_alive() and not got, "it waits while the other one runs"
    assert "waiting for it to stop" in capsys.readouterr().out
    held.close()
    waiter.join(5)
    assert got, "it takes the lock once the other one stops"
    got[0].close()
