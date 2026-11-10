# Qubotu

A personal team of AI agents that keeps working on your Mac.

Qubotu is an app you install and run on your own computer, like Codex or Claude Code, on your own model subscription. Unlike them, it isn't a window you work in: it runs in the background, and you manage it from your browser. You give each agent a job and a schedule. Agents carry their work from run to run, share what they find, ask each other for help, and build up a shared memory of what they learn about you. They come to you only when they're stuck.

Inspired by OpenMausBot, Instinct, and Grok Bot.

## Prerequisites

- **macOS.** Starting at login uses launchd. The rest may work on Linux.
- **The [Codex CLI](https://github.com/openai/codex), signed in.** Agents run on it, using your ChatGPT subscription.
  ```sh
  brew install codex        # or: npm install -g @openai/codex
  codex login
  ```
- **[Google Chrome](https://www.google.com/chrome/),** which agents browse with.
- **Optional: a [Fireworks](https://fireworks.ai) API key** for open models such as Qwen and DeepSeek, added in Qubotu's Settings.

## Install

```sh
curl -LsSf https://qubotu.com/install.sh | sh
```

The installer sets up [uv](https://docs.astral.sh/uv/) if needed, installs Qubotu, checks for the Codex CLI, and sets Qubotu to start at login (set `QUBOTU_NO_SERVICE=1` to skip that). If you already have uv:

```sh
uv tool install qubotu
qubotu service install          # optional: start at login
```

To upgrade, run the installer again, or `uv tool upgrade qubotu`.

## Quick start

Run `qubotu open`. Without the login service, run `qubotu serve --open` instead, and stop it with Ctrl-C. Each browser needs `qubotu open` once: it gives the browser a key, kept in a cookie, so agents on your computer can't use Qubotu's controls. After that, **http://127.0.0.1:8765** works directly.

Click **+ New** in the sidebar, give the agent a name and a job, and save. It runs once a day on your Codex default model unless you change that. Its findings and questions show up in its **Shared** tab, and anything it can't go on without shows as a red bar on every page.

## Documentation

- [Using Qubotu](docs/using.md): agents and their tabs, blockers, shared memory, and sites agents can use as you.
- [Running Qubotu](docs/running.md): commands, where your data lives, and troubleshooting.
- [How it works](docs/how-it-works.md): the security model, runs, tools, memory, and the browser.
- [Development](docs/development.md): working from a checkout, versioning, and releasing.
- [CHANGELOG.md](CHANGELOG.md) for what's been built, and [TODO.md](TODO.md) for what's planned.

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