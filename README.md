# Qubotu

A personal team of AI agents that keeps working on your laptop.

You create specialist agents, give each one a job, and leave them running on a schedule. Each agent carries its work from run to run, shares results in its own Shared tab, and asks you only when it really needs to. Agents can ask each other for help and build up a shared memory of what they learn about you. Everything runs locally; you manage it from your browser.

Inspired by OpenMausBot, Instinct, and Grok Bot.

## License

Qubotu is source-available under the [PolyForm Strict License 1.0.0](LICENSE).
Use is free of charge for the purposes permitted by that license, including:

- Any noncommercial purpose.
- The personal uses listed in the license, such as personal study and hobby
  projects, without any anticipated commercial application.
- Use by charitable organizations, educational institutions, public research
  organizations, public safety or health organizations, environmental protection
  organizations, and government institutions, regardless of funding source or
  obligations resulting from funding.

This license grants no rights to modify or redistribute Qubotu or create works
based on it. The full license governs; this summary does not grant additional
permissions.

Commercial use outside those permissions requires a separate paid license.
Contact [Emrah Diril](mailto:emrah@diril.org) for pricing and a commercial
agreement before beginning such use.

## Prerequisites

- **macOS.** Qubotu's other parts may work on Linux, but starting at login uses launchd.
- **The [Codex CLI](https://github.com/openai/codex),** signed in. Agents run on it.
  ```sh
  brew install codex        # or: npm install -g @openai/codex
  codex login
  ```
- **A model provider:**
  - **OpenAI:** your ChatGPT subscription, through `codex login`.
  - **Fireworks (optional):** for open models such as Qwen and DeepSeek, a [Fireworks](https://fireworks.ai) API key, added in Qubotu's Settings.
- **[uv](https://docs.astral.sh/uv/).** The installer sets it up if you don't have it.
- **[Google Chrome](https://www.google.com/chrome/).** Agents browse the web with it.

## Install

```sh
curl -LsSf https://qubotu.com/install.sh | sh
```

The installer installs uv if needed, installs Qubotu, checks for the Codex CLI, and sets Qubotu to start at login. To skip starting at login, set `QUBOTU_NO_SERVICE=1`.

If you already have uv:

```sh
uv tool install qubotu
qubotu service install          # optional: start at login
```

To upgrade, run the installer again, or `uv tool upgrade qubotu`.

## Using Qubotu

Open **http://127.0.0.1:8765**. If you didn't set up the login service, start Qubotu first with `qubotu serve --open`, and stop it with Ctrl-C.

### Create an agent
Click **+ New** in the sidebar and fill in:
- **Name:** no spaces, so other agents can ask it for help by name.
- **Job:** what it should keep working on, and how. Be specific about what you want to see from it and what to skip. The first line is its short summary, which other agents see when deciding who to ask for help.

**Model and schedule** is optional. By default an agent uses your Codex default model and runs once a day. Pick Fireworks there to use an open model; it needs a model ID such as `accounts/fireworks/models/...`.

### Work with an agent
Each agent has three tabs:
- **Chat:** talk to it directly. Replies stream in live, along with what it's doing: commands, web searches, tools.
- **Shared:** what it shares with you: findings, reports, tables, drafts, links, and questions. Answer questions right there. Mark outputs **Useful** or **Not useful**, with an optional comment; the agent sees your feedback on its next run. Mark them **Done** or **Dismiss** them to clear the list.
- **Settings:** change its job, model, or schedule, see its recent runs, start it on a fresh thread if its context gets muddled, or delete it along with its working folder.

The header has **Run now**, **Stop run**, **Pause**, and **Resume**. A paused agent skips its scheduled runs.

### When an agent is blocked
Some things an agent can't do without you, such as logging in to a site or answering a question it can't go on without. These show as a red bar at the top of every page, with the action right there (**Log in**, **Answer**, or **Not now**). The agent shows **blocked** in the sidebar, and the browser tab's title starts with the number of blocked requests, so you notice from another tab. Once you act, the agent carries on by itself. Everything else, like findings and questions that can wait, stays in the agent's Shared tab.

### Shared memory
What your agents have learned about you, plus anything you add yourself. Every agent can read it. Edit or remove anything that's wrong. Pinned memories are always included in agents' context.

### Sites agents can use as you
Some sites only show content to logged-in users, such as an X list. In **Settings**, enter the site (for example `x.com`) and click **Log in**. A Chrome window opens: log in as usual, then close it. Then open an agent's settings and tick the site under **Sites it can use as you**.

Agents browse those sites logged in as you, but read-only: Qubotu blocks anything that would post, like, or change something there. Each site has its own Chrome profile, which only that site's pages use, and agents never see your password. Consider a separate account for agents, because some sites (X among them) watch for automated browsing.

### Dashboard
Questions and important outputs waiting for you, each agent's status and next run, recent outputs, and recent collaboration. Blockers show in the bar at the top instead.

## Running Qubotu

```sh
qubotu serve [--open] [--port N]   # run in the foreground
qubotu service install             # start at login (macOS); restarts it if already installed
qubotu service uninstall           # stop starting at login
qubotu --version
```

Agents only run while Qubotu is running and your laptop is awake. Scheduled runs missed while the laptop was asleep collapse into a single run when it wakes.

Your data lives in `~/.qubotu/`:
- `qubotu.db`: agents, chats, outputs, and memory.
- `agents/<id>/`: each agent's working folder, including its `NOTES.md`.
- `models/`: the local embedding model.
- `browser/sites/<id>/`: the Chrome profile holding each site's login.
- `daemon.log`: the log when running as a login service.

Set `QUBOTU_HOME` to use a different folder, or `QUBOTU_PORT` for a different port.

### Troubleshooting
- **Runs fail with a Codex error:** run `codex login` in a terminal, then check **Settings** in Qubotu.
- **The login service can't find `codex`:** the service keeps the `PATH` from when it was installed. After installing or moving Codex, run `qubotu service install` again.
- **"Qubotu is already running":** another Qubotu process (often the login service) is using the same data folder. Use that one, or stop it with `qubotu service uninstall`. The other way around, if you start `qubotu serve` first, the login service waits quietly and takes over when you stop it.
- **An agent can't run a tool installed in your home folder** (for example node through nvm, or Python through uv): agents can't read your home folder. Tools in `/usr`, `/opt/homebrew`, and other system locations work.
- **Everything else:** check `~/.qubotu/daemon.log`.

## How it works

- **Local daemon.** One Python process (FastAPI, SQLite, Jinja, and htmx) runs the agents and serves the UI. Only one runs per data folder. It answers only requests addressed to `127.0.0.1` or `localhost`, and only state-changing requests from its own pages, so other websites can't drive your agents. The data folder is readable by your account only.
- **Agents.** Each agent runs on Codex app-server in its own persistent thread, with its own working folder. It can search the web and use the network. Its commands run under a Codex permission profile: they can read the system and write only their own folder and temp files, and they can't read your home folder (only `~/.gitconfig`, which git needs). So an agent can't read Qubotu's database, your Codex login, SSH keys, or the Keychain, even if a web page tries to talk it into it. Codex doesn't ask for approvals. Fireworks models use a custom Codex provider.
- **Runs.** Everything that makes an agent run (a chat message, its schedule, Run now, your answer to its question, a request from another agent) is first saved to an inbox, so work accepted before a restart still happens after it. Runs for one agent queue behind each other, and at most three agents run at once. Stop interrupts the running turn and cancels the queued ones. A turn that runs past 30 minutes is interrupted.
- **Tools.** Qubotu gives agents its own tools as Codex dynamic tools, an experimental app-server feature: `share_output` and `list_shared`, `ask_user`, `request_help`, and the memory tools. Codex fixes a thread's tools when it starts, so when the tool set changes, agents move to fresh threads. Their chat log, outputs, notes, and memory carry over.
- **Context.** Each run starts with only what changed since the agent's last run: its job (if edited), the team roster, the shared memory profile, and your feedback on its outputs. When Codex compacts a thread, the full context is sent again on the next run. Each run also includes older memories that match its message, beyond the pinned and most recent ones in the profile.
- **Collaboration.** `request_help` runs the other agent and delivers the result back to the requester. The request shows up in the helper's chat, and the dashboard lists recent requests. Requests chain at most two hops.
- **Shared memory.** SQLite rows with local embeddings (fastembed, `bge-small-en-v1.5`), recalled by combining numpy cosine similarity with FTS5 keyword search. Agents decide what to remember. Memory sits behind a small interface so a dedicated memory system can replace it later.
- **Browser.** The daemon drives Google Chrome with Playwright; agents only get page tools (`open_page`, `read_page`, `scroll_page`, `click_element`, `type_into`). Each run has one tab, closed when the run ends. Logged-out browsing uses a fresh context per run. A site the user connects gets a persistent profile under the data folder, which agents can't read. That profile is used only for its own site: navigations elsewhere are cancelled, and requests other than GET, HEAD, and OPTIONS to the site are blocked, except Cloudflare's bot check. Reads wait until the page's text stops changing, and `scroll_page` returns only text that wasn't there before, so feeds that drop old posts (like X) still read in full. Headless Chrome uses a normal Chrome user agent, because some sites refuse the headless one.

See [CHANGELOG.md](CHANGELOG.md) for what's been built and why, and [TODO.md](TODO.md) for what's planned.

## Development

From a checkout, prefix the `qubotu` commands above with `uv run`:

```sh
uv sync
uv run qubotu serve --open       # run in the foreground; Ctrl-C to stop
uv run qubotu service install    # or: run in the background, starting at login
uv run pytest                    # uses a fake Codex; no model calls
```

### Layout
- `src/qubotu/engine.py`: runs agent turns on Codex app-server, tracks progress, schedules recurring runs.
- `src/qubotu/codex.py`: JSON-RPC client for `codex app-server` over stdio.
- `src/qubotu/tools.py`: the tools agents get.
- `src/qubotu/collab.py`: requests between agents.
- `src/qubotu/memory.py`: shared memory with local embeddings and hybrid search.
- `src/qubotu/browser.py`: Chrome, driven for agents: page tools, site logins, read-only enforcement.
- `src/qubotu/prompts.py`: the instructions and messages agents receive.
- `src/qubotu/web.py` and `templates/`: the browser UI.
- `src/qubotu/cli.py`: the `qubotu` command and the login service.

### Versioning
Versions are `<major>.<work-day>.<increment>`, computed from Git history:
- [`version.json`](version.json) sets the major number and its baseline commit. We're on major `0` (pre-release), counted from the first commit, `d7a971d` ("qubotu: first public release", October 25, 2026).
- The baseline day is `0`. Each later date with commits advances the work-day number by one, however many idle days pass.
- The increment counts that date's commits, starting at `1`. Only first-parent history counts, using each commit's committer date.

Running from a Git checkout, the app computes the version from history and refreshes it whenever a commit lands, so the sidebar is never stale. Builds also write it into `src/qubotu/_version.py` (`build_backend/`), which installed copies without Git use. To start a new major, update `version.json` with the new major and a baseline commit; that commit becomes `<major>.0.1`.

### Releasing
Qubotu is published to PyPI as `qubotu`. [`PYPI.md`](PYPI.md) is the description shown on PyPI.

From a clean checkout of `main`:

```sh
uv run pytest
rm -rf dist && uv build          # the version comes from Git history
uv publish                       # needs a PyPI token: UV_PUBLISH_TOKEN or --token
```

Each release needs a new version, so commit before publishing again. To try the installer against a local build:

```sh
QUBOTU_PACKAGE=dist/qubotu-<version>-py3-none-any.whl QUBOTU_NO_SERVICE=1 sh install.sh
```
