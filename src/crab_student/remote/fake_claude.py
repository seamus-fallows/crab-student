#!/usr/bin/env python3
"""A stand-in for Claude Code (launch --fake-claude): it does a tiny task the way an agent would, so the
whole pipeline can be tested without spending Claude usage. It takes the command lines the machine gives
Claude Code: a new conversation (--session-id ID PROMPT), a resumed one (--resume ID [NOTE]) and a fork
(--resume ID --fork-session --session-id NEW PROMPT, which copies the conversation to the new id)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("CS_ROOT", "/root"))
SLEEP = float(os.environ.get("FAKE_CLAUDE_SLEEP", "1"))


def main(argv: list) -> int:
    folder = ROOT / ".claude" / "projects" / str(Path.cwd()).replace("/", "-")
    resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
    if resume and not (folder / (resume + ".jsonl")).exists():
        print("No conversation found with session ID: " + resume, file=sys.stderr)
        return 1
    fork = "--fork-session" in argv
    sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else resume
    transcript = folder / (sid + ".jsonl")
    folder.mkdir(parents=True, exist_ok=True)
    if fork:
        shutil.copy(str(folder / (resume + ".jsonl")), str(transcript))

    def note(kind: str, text: str) -> None:
        with open(str(transcript), "a") as f:
            f.write(json.dumps({"type": kind, "text": text, "at": time.time()}) + "\n")

    def step(*cmd: str) -> int:
        time.sleep(SLEEP)
        rc = subprocess.run(list(cmd)).returncode
        note("tool", f"{' '.join(cmd)} -> {rc}")
        return rc

    if resume and not fork:
        note("resumed", " ".join(argv))
    else:
        note("forked" if fork else "user", argv[-1])
        name = os.environ.get("AGENT_NAME", "agent")
        runs = Path("agent-runs") / name
        runs.mkdir(parents=True, exist_ok=True)
        step("agent-status", "working", "fake: starting")
        (runs / "result.txt").write_text("fake result\n")
        step("git", "add", "-A")
        step("git", "commit", "-q", "-m", "fake: result")
        step("git", "push", "-q")
        (ROOT / "outputs").mkdir(parents=True, exist_ok=True)
        (ROOT / "outputs" / "fake-output.bin").write_bytes(b"0" * 1024)
        if not (runs / "REPORT.md").exists():
            note("early-done", str(step("agent-status", "done", "too early")))  # (refused: no report yet)
        (runs / "REPORT.md").write_text(
            f"# {name}: fake run\n\nStatus: done\n\n## Summary\nA fake agent ran.\n"
        )
        step("git", "add", "-A")
        step("git", "commit", "-q", "-m", "fake: report")
        step("git", "push", "-q")
        step("agent-status", "done", "fake run finished")
    print("fake-claude: ready", flush=True)
    for line in sys.stdin:
        text = line.rstrip("\n")
        note("user", text)
        print(f'fake-claude: got "{text}"', flush=True)
    while True:  # (like the real screen, it stays until someone quits it)
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
