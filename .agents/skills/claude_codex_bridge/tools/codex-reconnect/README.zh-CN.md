# codex-reconnect

[English](README.md) | 简体中文

在 tmux 中长期守护 Codex CLI。网络波动或模型服务过载导致当前任务断连时，
它会等待当前 Codex Provider 路由恢复，然后自动输入 `continue`，让任务继续运行。

> 只处理已经造成任务中断的网络或模型服务问题；普通额度、上下文或“剩余 20%”
> 提醒不会触发自动继续。

## 安装

需要 Linux、macOS 或 WSL2，以及 tmux 和 Python 3.10+。

```sh
git clone https://github.com/SeemSeam/codex-reconnect.git
cd codex-reconnect
./install.sh
```

确保 `~/.local/bin` 位于 `PATH`。安装后重新打开一次 Codex，使其发现
`reconnect` skill。

## 使用

在 tmux 中正常打开 Codex：

```sh
tmux
codex
```

进入 Codex 后开启守护：

```text
$reconnect on
```

不再需要时关闭：

```text
$reconnect off
```

`on` 只守护当前 Codex 会话；不同 tmux pane 可以分别开启。
