# codex-reconnect

English | [简体中文](README.zh-CN.md)

Long-running protection for Codex CLI inside tmux. If a network interruption
or model-service overload disconnects the current task, it waits for the active
Codex provider route to recover and automatically submits `continue`.

> It only handles network or model-service failures that actually interrupt a
> task. Ordinary quota, context, or “20% remaining” notices do not trigger it.

## Install

Requires Linux, macOS, or WSL2 with tmux and Python 3.10+.

```sh
git clone https://github.com/SeemSeam/codex-reconnect.git
cd codex-reconnect
./install.sh
```

Make sure `~/.local/bin` is on `PATH`. Restart Codex once after installation so
it discovers the `reconnect` skill.

## Usage

Start Codex normally inside tmux:

```sh
tmux
codex
```

Inside Codex, turn protection on:

```text
$reconnect on
```

Turn it off when no longer needed:

```text
$reconnect off
```

`on` protects only the current Codex session. Different tmux panes can be
enabled independently.
