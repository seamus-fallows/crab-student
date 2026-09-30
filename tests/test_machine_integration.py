"""The machine side end to end, on Linux: the real cs.py, copied flat into <root>/crab-student as the upload
leaves it, runs setup, the watchdog, the fake Claude in tmux, status, send and the idle stop. "GitHub" is a
local bare repo and Vast a local HTTP server; tmux runs a server of its own (TMUX_TMPDIR). It needs tmux, git,
bash and pgrep (Linux CI installs tmux), takes about 75 s, and `pytest -m "not integration"` leaves it out."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_gh import git

REMOTE = Path(__file__).resolve().parents[1] / "src" / "crab_student" / "remote"
NAME, BRANCH, KEY = "itest", "agent/itest", "container-key-42"
DEPLOY_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nbm90IGEgcmVhbCBrZXk=\n-----END OPENSSH PRIVATE KEY-----\n"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform != "linux" or not all(shutil.which(t) for t in ("tmux", "git", "bash", "pgrep")),
        reason="needs Linux with tmux, git, bash and pgrep",
    ),
]


@pytest.fixture
def vast():
    """Vast's API as the watchdog calls it: a local HTTP server that records every request."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            headers = {k.lower(): v for k, v in self.headers.items()}
            requests.append((time.time(), self.command, self.path, headers, body))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = do_PUT

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", requests=requests)
    server.shutdown()
    server.server_close()


@pytest.fixture
def machine(tmp_path, vast):
    """The machine's files as the upload and the onstart command leave them, and cs.py's environment. At the
    end the watchdog's process group, the tmux server and anything else still running from D are killed."""
    root, bin_dir, bare = tmp_path / "root", tmp_path / "bin", github(tmp_path)
    d, repo = root / "crab-student", root / "work" / "proj"
    d.mkdir(parents=True)
    for name in ("cs.py", "fake_claude.py", "worker.md"):
        shutil.copy(REMOTE / name, d / name)
    sid = str(uuid.uuid4())
    config = dict(name=NAME, branch=BRANCH, repo_url=str(bare), repo_dir=str(repo), session_id=sid, model="")
    config.update(fake_claude=True, idle_minutes=1, limit_hours=0.5, notify_url="", secret_names=[])
    files = {
        "config.json": json.dumps(config),
        "BRIEF.md": "# Brief\n\nWrite result.txt and a report.\n",
        "claude.env": "",
        "secrets.env": "",
        "deploy_key": DEPLOY_KEY,
        "github_known_hosts": "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAInotarealkey\n",
        "vast.env": f"export CONTAINER_ID=42\nexport CONTAINER_API_KEY={KEY}\nexport CS_VAST_URL={vast.url}\n",
    }
    for name, text in files.items():
        (d / name).write_text(text)
    tmux_dir = tempfile.mkdtemp(prefix="cs-tmux-", dir="/tmp")  # (a socket path well under 108 bytes)
    env = dict(os.environ, CS_ROOT=str(root), CS_BIN=str(bin_dir), CS_PROFILE_D=str(tmp_path / "profile.d"))
    env.update(CS_TICK_SECONDS="1", TMUX_TMPDIR=tmux_dir, FAKE_CLAUDE_SLEEP="0", no_proxy="*")
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    for var in ("TMUX", "TMUX_PANE"):  # (inside tmux, a client would talk to that server instead)
        env.pop(var, None)
    yield SimpleNamespace(d=d, root=root, repo=repo, bare=bare, env=env, sid=sid, vast=vast)
    for pid in pids(f"{d}/cs.py watch"):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)  # (setup starts it in a session, so a process group, of its own)
    subprocess.run(["tmux", "kill-server"], env=env, capture_output=True, timeout=30)
    for pid in pids(str(d)):  # (a pane process that outlived its tmux server)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    shutil.rmtree(tmux_dir, ignore_errors=True)


def github(tmp_path: Path) -> Path:
    """'GitHub': a bare repo whose main and agent/itest hold one commit."""
    seed, bare = tmp_path / "seed", tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "README.md").write_text("# proj\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-q", "-m", "Initial commit")
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(seed, "push", "-q", str(bare), "main", f"main:{BRANCH}")
    return bare


def cs(m, *args: str, stdin: str = "", timeout: float = 60) -> subprocess.CompletedProcess:
    """`cs.py <args>` as the local side runs it over ssh: in D, with the machine's environment."""
    argv = [sys.executable, str(m.d / "cs.py"), *args]
    return subprocess.run(
        argv, input=stdin, cwd=m.d, env=m.env, capture_output=True, text=True, timeout=timeout
    )


def last_json(p: subprocess.CompletedProcess) -> dict:
    lines = p.stdout.strip().splitlines()
    assert lines, f"no output (exit {p.returncode}): {p.stderr}"
    return json.loads(lines[-1])


def tmux(m, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], env=m.env, capture_output=True, text=True, timeout=30)


def pids(pattern: str) -> list[int]:
    p = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=30)
    return [int(pid) for pid in p.stdout.split()]


def read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def worker(m) -> dict:
    return json.loads(read(m.d / "worker.json") or "{}")


def transcript(m) -> str:
    return "".join(read(p) for p in (m.root / ".claude" / "projects").glob(f"*/{m.sid}.jsonl"))


def diagnose(m) -> str:
    """The machine's logs, state, transcript and screen, for a failure message."""
    parts = [(p.name, read(p)) for p in sorted((m.d / "logs").glob("*.log"))]
    parts += [(name, read(m.d / name)) for name in ("worker.json", "state.json")]
    parts += [("transcript", transcript(m)), ("screen", tmux(m, "capture-pane", "-p", "-t", "worker").stdout)]
    return "".join(f"\n--- {name}\n{text[-3000:]}" for name, text in parts)


def wait_for(condition, seconds: float, what: str, m) -> None:
    deadline = time.time() + seconds
    while not condition():
        if time.time() > deadline:
            pytest.fail(f"timed out after {seconds:.0f} s waiting for {what}{diagnose(m)}")
        time.sleep(0.5)


def test_a_worker_runs_to_done_takes_a_message_and_its_machine_stops_when_idle(machine):
    m = machine
    # (a) setup: five steps, then {"ok": true} (the fake Claude needs no install, and tmux is there)
    p = cs(m, "setup", timeout=240)
    assert (p.returncode, last_json(p)) == (0, {"ok": True}), p.stdout + p.stderr + diagnose(m)
    steps = [line.split()[1] for line in p.stdout.splitlines() if line.startswith("step ")]
    assert steps == ["1/5", "2/5", "3/5", "4/5", "5/5"], p.stdout

    # (b) one watchdog, detached from setup; it keeps tmux session "worker" running the fake Claude, which
    # commits and pushes its result and report, then declares done
    watchdogs = pids(f"{m.d}/cs.py watch")
    assert len(watchdogs) == 1, f"watchdogs: {watchdogs}{diagnose(m)}"
    assert os.getsid(watchdogs[0]) == watchdogs[0], "the watchdog should run in a session of its own"
    assert tmux(m, "has-session", "-t", "worker").returncode == 0, diagnose(m)
    wait_for(lambda: worker(m).get("state") == "done", 90, "the worker to declare done", m)
    pushed = set(git(m.bare, "ls-tree", "-r", "--name-only", BRANCH).split())
    assert {f"agent-runs/{NAME}/result.txt", f"agent-runs/{NAME}/REPORT.md"} <= pushed, pushed

    # (c) status: done, and nothing uncommitted or unpushed
    st = last_json(cs(m, "status"))
    head = git(m.bare, "rev-parse", BRANCH).strip()[:7]
    assert st["worker"]["state"] == "done", st
    assert st["git"] == {"branch": BRANCH, "head": head, "dirty": 0, "unpushed": 0}, st
    assert st["stopping"] is None, st

    # (d) send types the text into the worker's Claude Code
    text, sent_at = "hello from the integration test", time.time()
    assert last_json(cs(m, "send", stdin=text + "\n")) == {"sent": True}
    wait_for(lambda: text in transcript(m), 30, "the message in the worker's transcript", m)

    # (e) then a minute without activity: the watchdog saves the work (here an uncommitted edit) and asks
    # Vast to stop the machine with the container's own key
    (m.repo / "README.md").write_text("edited on the machine\n")
    what = "the stop call to Vast (on a busy host outside a container, its CPU use counts as activity)"
    wait_for(lambda: m.vast.requests, 120, what, m)
    at, method, path, headers, body = m.vast.requests[0]
    assert (method, path, json.loads(body)) == ("PUT", "/api/v0/instances/42/", {"state": "stopped"}), path
    assert headers.get("authorization") == f"Bearer {KEY}", headers
    state = json.loads((m.d / "state.json").read_text())
    assert (state["stopping"], state["stop_reason"]) == ("idle", "idle"), state
    idle = at - state["last_activity"]  # (the send was the last activity, unless CPU noise came later)
    assert state["last_activity"] >= sent_at and 60 <= idle < 75, f"idle {idle:.1f} s: {state}"
    assert git(m.bare, "show", f"{BRANCH}:README.md") == "edited on the machine\n", "the edit was not saved"
