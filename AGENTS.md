# AGENTS.md

Directives for AI agents working on Qubotu. Claude Code keeps an identical copy of this file as its project memory (`MEMORY.md` in its memory folder for this project). When you change one, make the same change in the other, so they never differ.

## Product

- **Clear UX comes first.** It should always be obvious what to put where and what will happen. That clarity is the point of Qubotu, so keep it as features are added.
- **Few fields.** Before adding a setting, check whether a default would do. Put optional settings behind a collapsed section with sensible defaults.
- **Local first.** Qubotu is a local daemon on the user's computer that serves a plain browser UI. Agents, their browser sessions, and their data stay on that computer. Running in the cloud may come later. Don't add a PWA manifest or a service worker, and don't suggest Electron. A native wrapper (likely Tauri) is deferred, and the daemon must keep running without any UI window.
- **Memory is easy to see and change.** The user can read, add, edit, and remove what's in shared memory from one screen. Never show which memory backend runs underneath.
- **Blockers stand out.** Anything an agent can't go on without (a login, a blocking question) shows prominently on every page. Notifications that can wait, like findings, stay in the agent's Shared tab.
- **Agents browse like a person:** at a normal pace, with the user's own Chrome identity.
- **Site-specific logic belongs in tools,** such as custom tools the user approves, not in Qubotu's code. Qubotu's browser stays generic.

## Code and data

- **No migration code.** No schema sync on startup, no one-time upgrade steps, and no tests or fixtures for old databases. When the schema changes, edit `SCHEMA` in `db.py`. Then apply the change to the user's database (`~/.qubotu/qubotu.db`) offline: stop the daemon, back the database up, and run a one-off command such as `ALTER TABLE`. New tables need nothing, because `CREATE TABLE IF NOT EXISTS` creates them on startup.
- **Tests never touch the user's data folder.** Point paths such as `config.UI_KEY_PATH` and `config.BROWSER_DIR` at a temporary folder.

## Workflow

- **The user owns git and runs the app.** Don't commit, stage, or push unless asked. Don't start `qubotu serve` unprompted.
- **Verify with the test suite** (`uv run pytest`). It includes browser tests that drive the installed Chrome against a local test server. If a live server is truly needed, stop it when done.
- **End finished work with a suggested commit message** in `git log` style: a short lowercase subject, and a body only when the change needs explaining. No `Co-Authored-By` or other attribution lines.

## TODO.md

- **Only work the user asked for or agreed to.** Suggest other ideas in chat, and add them only if the user says yes.
- **High level only.** One or two sentences per item, in priority order, tagged with what it touches. Leave design details for when the item is picked up and discussed.

## License

- Qubotu is under the PolyForm Strict License 1.0.0 (`LicenseRef-PolyForm-Strict-1.0.0` in `pyproject.toml`, since it isn't on the SPDX list). Personal and other noncommercial use is free. Business and commercial use requires a paid license (contact emrah@diril.org). Distribution and modified versions aren't allowed.
- Anywhere the license is described (docs, the PyPI page, the UI), say it's free for personal and noncommercial use and that business and commercial use requires a paid license. Never call it open source or just "free".
- Qubotu is published on public PyPI as `qubotu`, and its repository is public.
