# Development

From a checkout, prefix the [`qubotu` commands](running.md) with `uv run`:

```sh
uv sync
uv run qubotu serve --open       # run in the foreground; Ctrl-C to stop
uv run pytest                    # uses a fake Codex; no model calls
```

## Layout
- `src/qubotu/engine.py`: runs agent turns on Codex app-server, tracks progress, schedules recurring runs.
- `src/qubotu/codex.py`: JSON-RPC client for `codex app-server` over stdio.
- `src/qubotu/tools.py`: the tools agents get.
- `src/qubotu/collab.py`: requests between agents.
- `src/qubotu/memory.py`: shared memory with local embeddings and hybrid search.
- `src/qubotu/browser.py`: Chrome for agents: page tools, site logins, read-only enforcement.
- `src/qubotu/prompts.py`: the instructions and messages agents receive.
- `src/qubotu/web.py` and `templates/`: the browser UI.
- `src/qubotu/cli.py`: the `qubotu` command and the login service.

## Versioning
Versions are `<major>.<work-day>.<increment>`, computed from first-parent Git history by committer date. [`version.json`](../version.json) sets the major (`0`, pre-release) and its baseline commit (`d7a971d`, October 25, 2026), which is day `0`. Each later date with commits adds one to the work day, however many idle days pass, and the increment counts that date's commits from `1`.

A checkout computes the version live, so the sidebar is never stale. Builds write it into `src/qubotu/_version.py` (`build_backend/`) for installs without Git. To start a new major, set it and a new baseline commit in `version.json`. That commit becomes `<major>.0.1`.

## Releasing
Qubotu is published to PyPI as `qubotu`, with [`PYPI.md`](../PYPI.md) as its description. From a clean checkout of `main`:

```sh
uv run pytest
rm -rf dist && uv build          # the version comes from Git history
uv publish                       # needs a PyPI token: UV_PUBLISH_TOKEN or --token
```

Each release needs a new version, so commit before publishing again. To try the installer against a local build:

```sh
QUBOTU_PACKAGE=dist/qubotu-<version>-py3-none-any.whl QUBOTU_NO_SERVICE=1 sh install.sh
```
