"""The version shown in the app.

From a Git checkout it's computed from history the same way the build does
(build_backend/versioning.py), and recomputed whenever a commit lands, so it
never goes stale between installs. Without Git (an installed copy), it's the
version written at build time.
"""

import importlib.util
import os
from pathlib import Path

from ._version import __version__ as built_version

ROOT = Path(__file__).resolve().parents[2]
_cache: dict = {}


def _versioning():
    if "module" not in _cache:
        spec = importlib.util.spec_from_file_location(
            "qubotu_versioning", ROOT / "build_backend" / "versioning.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _cache["module"] = module
    return _cache["module"]


def current_version(root: Path = ROOT) -> str:
    # .git/logs/HEAD is appended to on every commit, checkout, and reset.
    head_log = root / ".git" / "logs" / "HEAD"
    if not (root / "version.json").is_file() or not head_log.is_file():
        return built_version
    key = (root, os.stat(head_log).st_mtime_ns)
    if _cache.get("key") != key:
        try:
            _cache["version"] = _versioning().git_version(root)
        except Exception:
            # Only a display string: a broken Git checkout must not take down every page.
            _cache["version"] = built_version
        _cache["key"] = key
    return _cache["version"]
