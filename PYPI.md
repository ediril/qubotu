# Qubotu

A personal team of AI agents that keeps working on your laptop.

Create specialist agents, give each one a job, and leave them working on a schedule. They share results in a Shared tab, ask you only when they need to, help each other, and build up a shared memory of what they learn about you. Everything runs locally; you manage it from your browser.

## Install

```sh
curl -LsSf https://qubotu.com/install.sh | sh
```

Or, if you already have [uv](https://docs.astral.sh/uv/):

```sh
uv tool install qubotu
```

Qubotu runs agents on the [Codex CLI](https://github.com/openai/codex), so install it and sign in with `codex login`. OpenAI models use your ChatGPT subscription; open models such as Qwen and DeepSeek use a Fireworks API key.

## Use

```sh
qubotu serve --open             # run it and open it in your browser
qubotu open                     # open it in your browser (needed once per browser)
qubotu service install          # start automatically at login (macOS)
```

Your data stays in `~/.qubotu/`.

Qubotu is free for personal and other noncommercial use under the PolyForm Strict License 1.0.0. Business and other commercial use requires a paid license: contact emrah@diril.org. It is not open source.
