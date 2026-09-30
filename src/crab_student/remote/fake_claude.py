#!/usr/bin/env python3
"""A stand-in for Claude Code (launch --fake-claude): it does a tiny task the way a worker would, so the
whole pipeline can be tested without spending Claude usage."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("CS_ROOT", "/root"))
SLEEP = float(os.environ.get("FAKE_CLAUDE_SLEEP", "1"))


def main(argv: list) -> int:
    flag = "--resume" if "--resume" in argv else "--session-id"
    sid = argv[argv.index(flag) + 1]
    transcript = ROOT / ".claude" / "projects" / str(Path.cwd()).replace("/", "-") / (sid + ".jsonl")
    if flag == "--resume" and not transcript.exists():
        print("No conversation found with session ID: " + sid, file=sys.stderr)
        return 1
    transcript.parent.mkdir(parents=True, exist_ok=True)

    def note(kind: str, text: str) -> None:
        with open(str(transcript), "a") as f:
            f.write(json.dumps({"type": kind, "text": text, "at": time.time()}) + "\n")

    def step(*cmd: str) -> int:
        time.sleep(SLEEP)
        rc = subprocess.run(list(cmd)).returncode
        note("tool", "%s -> %d" % (" ".join(cmd), rc))
        return rc

    if flag == "--resume":
        note("resumed", " ".join(argv))
    else:
        note("user", argv[-1])
        name = os.environ.get("CRAB_STUDENT_NAME", "worker")
        runs = Path("agent-runs") / name
        runs.mkdir(parents=True, exist_ok=True)
        step("crab-status", "working", "fake: starting")
        (runs / "result.txt").write_text("fake result\n")
        step("git", "add", "-A")
        step("git", "commit", "-q", "-m", "fake: result")
        step("git", "push", "-q")
        (ROOT / "outputs").mkdir(parents=True, exist_ok=True)
        (ROOT / "outputs" / "fake-output.bin").write_bytes(b"0" * 1024)
        note("early-done", str(step("crab-status", "done", "too early")))  # (refused: no report yet)
        (runs / "REPORT.md").write_text(
            "# %s: fake run\n\nStatus: done\n\n## Summary\nA fake worker ran.\n" % name
        )
        step("git", "add", "-A")
        step("git", "commit", "-q", "-m", "fake: report")
        step("git", "push", "-q")
        step("crab-status", "done", "fake run finished")
    print("fake-claude: ready", flush=True)
    for line in sys.stdin:
        note("user", line.rstrip("\n"))
        print('fake-claude: got "%s"' % line.rstrip("\n"), flush=True)
    while True:  # (like the real screen, it stays until someone quits it)
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
