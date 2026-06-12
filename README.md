# Project BareMetal-Tmux

A decoupled, stateful local MCP (Model Context Protocol) orchestration harness.
It exposes a persistent host `tmux` session to MCP clients — GitHub Copilot CLI,
ForgeCode, VS Code — over the stdio transport. No containers, no network: raw
JSON-RPC text streams straight into bare-metal shell execution.

Interactive terminal state survives agent crashes, context compaction reloads,
and framework restarts, because the shell lives in a detached `tmux` session
(`local_agent_workspace`) owned by the host — not by the agent process.

## Architecture

```
Upstream Client (Copilot CLI / ForgeCode / VS Code)
  │  spawns subprocess, speaks JSON-RPC over stdio
  ▼
Local MCP Server  (server.py — Python 3.11+, fastmcp)
  │  validates schemas, applies SR-01 safety checks, strips ANSI
  ▼
Host OS Shell  (persistent detached tmux session, 20k-line scrollback)
```

## MCP Tools

| Tool | Arguments | Purpose |
|---|---|---|
| `execute_command` | `command: str`, `wait_s: float = 0` | Dispatch a literal shell command into the persistent pane, followed by a carriage return (`C-m`). Fire-and-forget by default; pass `wait_s` > 0 (capped at 30) to sleep then return the trailing buffer in the same call. |
| `read_terminal_buffer` | `lines: int = 100` (max 20000) | Capture trailing scrollback from the pane, with ANSI/OSC/control sequences stripped so payloads always encode cleanly into JSON-RPC. |
| `send_control_signal` | `signal: str = "SIGINT"` | Send `SIGINT` (Ctrl+C), `SIGTSTP`, `SIGQUIT`, or `EOF` to break blocking processes without killing the harness. |

The session is created lazily on first tool invocation (FR-01) with a
20,000-line history limit.

## Safety Guardrails (SR-01)

Execution is un-sandboxed on the host, so the server refuses destructive
commands **before** they reach the tmux buffer:

- Recursive `rm` aimed at `/` or top-level system roots (`/usr`, `/etc`, …),
  including `sudo`-wrapped and `&&`/`;`-chained variants
- `--no-preserve-root`, `mkfs.*`, `dd of=/dev/...`, redirects onto block
  devices, fork-bomb signatures

Workspace-scoped destruction (e.g. `rm -rf ./build`, `/tmp/...`) passes through.

## Requirements

- **Windows host**: WSL distro `Ubuntu-24.04` with `tmux` installed
  (the server runs inside WSL; clients launch it via `wsl.exe`)
- **Linux/macOS host**: just `tmux` on `PATH` — drop the `wsl.exe` wrapper and
  run `uv run python server.py` directly
- [uv](https://docs.astral.sh/uv/) (installed user-locally, e.g.
  `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Python ≥ 3.11 (resolved by uv)

## Setup

Dependencies are isolated in a uv-managed venv. On WSL, keep the venv on ext4
(not `/mnt/c`) for speed:

```bash
cd /mnt/c/Users/troll/Python/tmux-harness
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/tmux-harness"
uv sync
```

Run the smoke test (28 checks: ANSI stripping, SR-01 guardrails, live tmux
round-trips against a throwaway session):

```bash
TMUX_HARNESS_SESSION=harness_smoke_test uv run python tests/smoke_test.py
```

## Client Integration

### GitHub Copilot CLI

Two options:

1. **Custom agent (recommended)** — [.github/agents/tmux.agent.md](.github/agents/tmux.agent.md)
   is a self-contained agent profile: it defines the MCP server in its
   frontmatter and allowlists **only** the three harness tools (no shell, no
   file edits, no web). Use it with:

   ```text
   /agent                          # pick "tmux" interactively
   copilot --agent=tmux -p "run the test suite and watch for failures"
   ```

2. **Global MCP server** — merge [mcp-config.copilot.json](mcp-config.copilot.json)
   into `~/.copilot/mcp-config.json` to expose the harness tools in every
   session (all built-in tools remain available).

### ForgeCode

[.mcp.json](.mcp.json) sits at the project root and is picked up automatically
when ForgeCode runs from this directory.

### VS Code

Add the same server block from [.mcp.json](.mcp.json) to `.vscode/mcp.json`.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `TMUX_HARNESS_SESSION` | `local_agent_workspace` | Name of the persistent tmux session |
| `UV_PROJECT_ENVIRONMENT` | `.venv` in project | Venv location (keep on ext4 under WSL) |

## Project Layout

```
server.py                     MCP server (tools, SR-01 checks, ANSI stripping)
pyproject.toml                uv project: mcp[cli] + fastmcp
tests/smoke_test.py           28-check verification suite
.github/agents/tmux.agent.md  Copilot CLI custom agent (harness tools only)
mcp-config.copilot.json       Copilot CLI global MCP config
.mcp.json                     ForgeCode project-root MCP config
```
