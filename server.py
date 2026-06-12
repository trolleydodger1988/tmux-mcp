#!/usr/bin/env python3
"""Project BareMetal-Tmux — Stateful Local MCP Orchestration Harness.

Exposes a persistent host tmux session to MCP clients (GitHub Copilot CLI,
ForgeCode) over the stdio transport. State survives agent crashes, context
compaction reloads, and framework restarts (FR-01).

Run: uv run python server.py
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Stateful-Tmux-Harness")

SESSION_NAME = os.environ.get("TMUX_HARNESS_SESSION", "local_agent_workspace")
HISTORY_LIMIT = "20000"
PANE_WIDTH = "220"
PANE_HEIGHT = "50"
TMUX_TIMEOUT_S = 10
MAX_CAPTURE_LINES = 20_000
MAX_WAIT_S = 30.0  # ceiling for execute_command's optional wait_s

# ---------------------------------------------------------------------------
# SR-01: Safety guardrails — destructive syntax must fail BEFORE reaching the
# tmux buffer, returning a strict execution error block.
# ---------------------------------------------------------------------------

#: Filesystem roots that may never be the target of a recursive removal.
_CRITICAL_ROOTS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64",
    "/opt", "/proc", "/root", "/sbin", "/srv", "/sys", "/usr", "/var",
}

#: Simple destructive signatures checked against the raw command string.
_FORBIDDEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"--no-preserve-root"), "explicit root-preservation override"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "filesystem format (mkfs)"),
    (re.compile(r"\bdd\b[^;|&]*\bof=/dev/"), "raw dd write to a block device"),
    (re.compile(r">\s*/dev/(sd|hd|vd|nvme|mmcblk)"), "redirect onto a block device"),
    (re.compile(r":\(\)\s*\{"), "fork bomb signature"),
)

#: Wrapper commands skipped when locating the effective binary in a segment.
_WRAPPERS = {"sudo", "env", "nohup", "nice", "time"}


def _is_critical_path(token: str) -> bool:
    """True if token resolves to / or a top-level system directory (incl. /usr/* forms)."""
    stripped = token.rstrip("/*")
    return token in ("/", "/*") or (stripped or "/") in _CRITICAL_ROOTS


def _segment_is_root_removal(segment: str) -> bool:
    """Detect `rm` invocations with a recursive flag aimed at a critical root."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        # Unparseable quoting — refuse to certify it as safe if it names rm and /
        return bool(re.search(r"\brm\b.*\s/(\s|\*|$)", segment))
    # Skip leading wrapper commands (sudo rm -rf / ...)
    while tokens and tokens[0] in _WRAPPERS:
        tokens = tokens[1:]
    if not tokens or tokens[0] != "rm":
        return False
    recursive = False
    targets: list[str] = []
    for tok in tokens[1:]:
        if tok in ("--recursive", "--force"):
            recursive = recursive or tok == "--recursive"
        elif tok.startswith("-") and not tok.startswith("--"):
            recursive = recursive or "r" in tok.lower()
        elif not tok.startswith("-"):
            targets.append(tok)
    return recursive and any(_is_critical_path(t) for t in targets)


def _safety_check(command: str) -> None:
    """SR-01 blocklist. Raises ValueError with a strict execution error block."""
    for pattern, reason in _FORBIDDEN_PATTERNS:
        if pattern.search(command):
            raise ValueError(
                f"EXECUTION BLOCKED (SR-01): {reason}. "
                "Command refused before reaching the tmux buffer."
            )
    for segment in re.split(r"[;&|]+|\n", command):
        if _segment_is_root_removal(segment.strip()):
            raise ValueError(
                "EXECUTION BLOCKED (SR-01): unconstrained removal targeting a "
                "critical filesystem root. Command refused before reaching the "
                "tmux buffer."
            )


# ---------------------------------------------------------------------------
# Phase 2: ANSI / control-sequence stripping so buffer payloads always encode
# cleanly into the JSON-RPC response stream.
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(
    r"""
      \x1b\[[0-?]*[ -/]*[@-~]                # CSI sequences (colors, cursor)
    | \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?     # OSC sequences (titles, links)
    | \x1b[PX^_][^\x1b]*(?:\x1b\\)?          # DCS / SOS / PM / APC strings
    | \x1b[@-Z\\-_]                          # bare C1 escapes
    | \x9b[0-?]*[ -/]*[@-~]                  # 8-bit CSI
    """,
    re.VERBOSE,
)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _sanitize(text: str) -> str:
    """Strip ANSI escapes and non-printable control bytes; normalize newlines."""
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CTRL_RE.sub("", text)


# ---------------------------------------------------------------------------
# Tmux session plumbing
# ---------------------------------------------------------------------------


def _tmux(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=TMUX_TIMEOUT_S
    )


def _ensure_session() -> None:
    """FR-01: lazily spin up the named, detached session on first invocation."""
    if not shutil.which("tmux"):
        raise RuntimeError("Tmux is missing from host path.")
    check = _tmux("has-session", "-t", f"={SESSION_NAME}")
    if check.returncode != 0:
        # Single invocation: raise the global history limit BEFORE the first
        # pane is created so pane 0 actually inherits the 20k scrollback.
        created = _tmux(
            "set-option", "-g", "history-limit", HISTORY_LIMIT, ";",
            "new-session", "-d", "-s", SESSION_NAME,
            "-x", PANE_WIDTH, "-y", PANE_HEIGHT,
        )
        if created.returncode != 0:
            raise RuntimeError(
                f"Failed to create tmux session: {created.stderr.strip()}"
            )


# ---------------------------------------------------------------------------
# MCP tool surface (JSON-RPC schemas derived from type hints)
# ---------------------------------------------------------------------------


@mcp.tool()
def execute_command(command: str, wait_s: float = 0.0) -> str:
    """Dispatch a raw shell command string into the persistent background
    workspace pane (FR-02). The text is sent literally, then a carriage return
    (C-m) is appended to ensure statement completion.

    By default output is NOT returned — poll read_terminal_buffer to observe
    results. Pass wait_s > 0 to sleep that many seconds (capped at 30.0)
    after dispatch and get the trailing buffer back in the same call —
    ideal for quick commands. Long jobs should still dispatch, then poll.
    """
    _safety_check(command)
    _ensure_session()
    literal = _tmux("send-keys", "-t", SESSION_NAME, "-l", "--", command)
    if literal.returncode != 0:
        raise RuntimeError(f"tmux send-keys failed: {literal.stderr.strip()}")
    enter = _tmux("send-keys", "-t", SESSION_NAME, "C-m")
    if enter.returncode != 0:
        raise RuntimeError(f"tmux carriage return failed: {enter.stderr.strip()}")
    if wait_s > 0:
        wait = min(float(wait_s), MAX_WAIT_S)
        time.sleep(wait)
        return (
            f"Dispatched to session '{SESSION_NAME}'. "
            f"Buffer after {wait:g}s wait:\n{_capture_buffer(100)}"
        )
    return f"Dispatched command to session '{SESSION_NAME}'. Status: 0"


def _capture_buffer(lines: int) -> str:
    """Capture, sanitize, join wrapped rows, and trim trailing padding."""
    lines = max(1, min(int(lines), MAX_CAPTURE_LINES))
    _ensure_session()
    res = _tmux("capture-pane", "-p", "-J", "-t", SESSION_NAME, "-S", f"-{lines}")
    if res.returncode != 0:
        raise RuntimeError(f"tmux capture-pane failed: {res.stderr.strip()}")
    text = _sanitize(res.stdout)
    # -J joins hard-wrapped rows but preserves trailing spaces; strip those
    # and drop blank viewport-padding rows so payloads stay compact.
    rows = [row.rstrip() for row in text.split("\n")]
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows)


@mcp.tool()
def read_terminal_buffer(lines: int = 100) -> str:
    """Capture trailing scrollback from the active tmux pane (FR-03).

    Hard-wrapped rows are joined and trailing blank viewport padding is
    trimmed before the payload is returned.

    Args:
        lines: Number of trailing scrollback lines to capture (default 100,
            max 20000).
    """
    return _capture_buffer(lines)


_SIGNAL_KEYMAP = {
    "SIGINT": "C-c",   # break blocking processes / stuck test loops
    "SIGTSTP": "C-z",  # suspend foreground job
    "SIGQUIT": "C-\\",  # quit with core dump
    "EOF": "C-d",      # end-of-input for interactive readers
}


@mcp.tool()
def send_control_signal(signal: str = "SIGINT") -> str:
    """Issue a control signal keystroke to the persistent pane (FR-04), e.g.
    SIGINT (Ctrl+C) to break out of a blocking process without killing the
    harness. Supported: SIGINT, SIGTSTP, SIGQUIT, EOF.
    """
    key = _SIGNAL_KEYMAP.get(signal.upper().strip())
    if key is None:
        raise ValueError(
            f"Unsupported signal '{signal}'. Choose from {sorted(_SIGNAL_KEYMAP)}."
        )
    _ensure_session()
    res = _tmux("send-keys", "-t", SESSION_NAME, key)
    if res.returncode != 0:
        raise RuntimeError(f"tmux signal dispatch failed: {res.stderr.strip()}")
    return f"Sent {signal.upper().strip()} ({key}) to session '{SESSION_NAME}'."


if __name__ == "__main__":
    # FR-05: stdio transport — raw text blocks over stdout for upstream
    # orchestrators to compact natively.
    mcp.run()
