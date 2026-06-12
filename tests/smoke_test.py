"""Smoke test for the Stateful-Tmux-Harness MCP server.

Runs against a throwaway tmux session (harness_smoke_test) so the real
workspace session is untouched. Execute inside WSL:

    TMUX_HARNESS_SESSION=harness_smoke_test uv run python tests/smoke_test.py
"""

import os
import subprocess
import sys
import time

os.environ.setdefault("TMUX_HARNESS_SESSION", "harness_smoke_test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    tag = "PASS" if condition else "FAIL"
    print(f"[{tag}] {name}" + (f" :: {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# --- Phase 2: ANSI / control stripping -------------------------------------
dirty = "\x1b[31mred\x1b[0m \x1b]0;title\x07link\x1b\\ bell\x07 tab\tkeep\r\nnl"
clean = server._sanitize(dirty)
check("ansi CSI stripped", "\x1b" not in clean and "red" in clean)
check("OSC title stripped", "title" not in clean)
check("bell byte stripped", "\x07" not in clean)
check("tab + newline preserved", "\t" in clean and "\n" in clean)
check("crlf normalized", "\r" not in clean)

# --- SR-01 guardrails -------------------------------------------------------
def blocked(cmd: str) -> bool:
    try:
        server._safety_check(cmd)
        return False
    except ValueError:
        return True


check("blocks rm -rf /", blocked("rm -rf /"))
check("blocks sudo rm -fr /usr", blocked("sudo rm -fr /usr"))
check("blocks chained root wipe", blocked("echo hi && rm -rf /*"))
check("blocks --no-preserve-root", blocked("rm -r --no-preserve-root /x"))
check("blocks mkfs", blocked("mkfs.ext4 /dev/sda1"))
check("blocks dd to block device", blocked("dd if=/dev/zero of=/dev/sda"))
check("allows rm -rf ./build", not blocked("rm -rf ./build"))
check("allows rm -rf /tmp/scratch", not blocked("rm -rf /tmp/scratch"))
check("allows ls -la /", not blocked("ls -la /"))
check("allows grep -r / pattern", not blocked("grep -rn 'a/b' src/"))

# --- FR-01..FR-04: live tmux round-trip --------------------------------------
session = server.SESSION_NAME
subprocess.run(["tmux", "kill-session", "-t", f"={session}"], capture_output=True)

try:
    msg = server.execute_command("echo HARNESS_OK_$((6 * 7))")
    check("execute_command dispatches", "Dispatched" in msg)

    found = ""
    for _ in range(20):
        time.sleep(0.25)
        found = server.read_terminal_buffer(lines=50)
        if "HARNESS_OK_42" in found:
            break
    check("round-trip output captured", "HARNESS_OK_42" in found, found[-300:])

    persists = subprocess.run(
        ["tmux", "has-session", "-t", f"={session}"], capture_output=True
    )
    check("session persists detached", persists.returncode == 0)

    limit = subprocess.run(
        ["tmux", "display-message", "-p", "-t", session, "#{history_limit}"],
        capture_output=True, text=True,
    )
    check("history limit is 20000", limit.stdout.strip() == "20000", limit.stdout)

    server.execute_command("sleep 30")
    time.sleep(0.5)
    sig = server.send_control_signal("SIGINT")
    check("signal dispatch reports", "SIGINT" in sig)
    time.sleep(0.7)
    server.execute_command("echo AFTER_SIGINT_$((10 + 5))")
    after = ""
    for _ in range(20):
        time.sleep(0.25)
        after = server.read_terminal_buffer(lines=50)
        if "AFTER_SIGINT_15" in after:
            break
    check("SIGINT broke blocking sleep", "AFTER_SIGINT_15" in after, after[-300:])

    big = server.read_terminal_buffer(lines=999_999)
    check("oversized lines clamped", isinstance(big, str))

    server.execute_command("printf 'W%.0s' {1..300}; echo")
    joined = ""
    for _ in range(20):
        time.sleep(0.25)
        joined = server.read_terminal_buffer(lines=50)
        if "W" * 300 in joined:
            break
    check("wrapped rows joined (-J)", "W" * 300 in joined, joined[-200:])
    check("trailing padding trimmed", bool(joined) and joined.splitlines()[-1].strip() != "")
    check("no per-line trailing spaces", all(r == r.rstrip() for r in joined.splitlines()))

    res = server.execute_command("echo WAIT_ONESHOT_$((2 ** 5))", wait_s=2.0)
    check("wait_s one-shot returns buffer", "WAIT_ONESHOT_32" in res, res[-300:])
    check("wait_s reports wait duration", "Buffer after 2s wait" in res, res[:80])
    check("wait_s cap constant", server.MAX_WAIT_S == 30.0)
finally:
    subprocess.run(["tmux", "kill-session", "-t", f"={session}"], capture_output=True)

print()
if FAILURES:
    print(f"SMOKE TEST FAILED: {len(FAILURES)} failure(s): {FAILURES}")
    sys.exit(1)
print("SMOKE TEST PASSED: all checks green")
