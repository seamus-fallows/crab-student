#!/usr/bin/env python3
"""crab-student on the machine: setup, the watchdog, Claude Code in tmux, and the commands the local side
runs over ssh (each prints one JSON object on its last line). Python 3.8+ standard library only."""

from __future__ import annotations

import glob
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("CS_ROOT", "/root"))
D = ROOT / "crab-student"
OUTPUTS = ROOT / "outputs"
BIN = Path(os.environ.get("CS_BIN", "/usr/local/bin"))
PROFILE_D = Path(os.environ.get("CS_PROFILE_D", "/etc/profile.d"))
CLAUDE = ROOT / ".local" / "bin" / "claude"
TICK = float(os.environ.get("CS_TICK_SECONDS", "60"))
PY = sys.executable or "python3"
CPU_BUSY_CORES, GRACE = 0.25, 300
QUIET_MINUTES = 20  # a "working" worker silent this long gets a check-in (longer than its longest sleep)
DONE_MINUTES = 15  # a finished worker's machine stops after this long without activity
GIT_BUSY = ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache"}
STEPS = ("environment", "repository", "worker settings", "Claude Code", "starting the worker")

FIRST_PROMPT = (
    "You are a crab-student worker. Your task brief is in /root/crab-student/BRIEF.md and your rules are in "
    '~/.claude/CLAUDE.md. Read both, run crab-status working "<first step>", then start.'
)
LOST_PROMPT = FIRST_PROMPT + (
    " Your previous conversation could not be restored: read your REPORT.md and git log "
    "to see how far you got, then continue."
)
RESTART_NOTE = (
    "crab-student: Claude Code was restarted (the machine was started again, the host rebooted, or Claude "
    "Code crashed). Background jobs from before are gone. If you were in the middle of the task, check "
    "where things stand (git log, your logs, checkpoints) and continue. If you had finished, reply "
    '"ok" and wait.'
)
NUDGE = (
    "crab-student: nobody has answered for {minutes} minutes. Choose what you think is best, write the choice "
    'and your reasons in REPORT.md under "Decisions", run crab-status working, and continue.'
)
WARN = (
    "crab-student: about 10 minutes of machine time are left before the time limit ({limit}). Commit and push "
    "your work and bring REPORT.md up to date now. The machine will stop itself."
)
CHECK_IN = (
    "crab-student: you have been quiet for {minutes} minutes while your status says working. Check on your "
    "running jobs and carry on with the task; if you are finished, complete the report and run crab-status done."
)
TMUX_CONF = (
    'set -g mouse on\nset -g history-limit 50000\nset -sg escape-time 10\nset -g default-terminal "screen-256color"\n'
    'set -g status-left "[{name}] "\nset -g status-left-length 40\nset -g status-right "detach: Ctrl-b d "\n'
)
SETTINGS = {
    "env": {"DISABLE_AUTOUPDATER": "1", "IS_SANDBOX": "1"},
    "permissions": {
        "deny": [
            "Bash(git push --no-verify:*)",
            "Bash(git commit --no-verify:*)",
            "Bash(git commit -n:*)",
            "Bash(git config core.hooksPath:*)",
            "Bash(git -c core.hooksPath:*)",
            "Bash(git remote set-url:*)",
            "Bash(git remote add:*)",
            "Bash(git push --mirror:*)",
        ]
    },
}


# ---- small helpers -------------------------------------------------------------------------------


def cfg() -> dict:
    return json.loads((D / "config.json").read_text())


def write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.chmod(str(tmp), mode)
    os.replace(str(tmp), str(path))


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def out(obj: dict) -> int:
    print(json.dumps(obj), flush=True)
    return 0


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def last_line(text: str) -> str:
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    return lines[-1] if lines else "no output"


def fmt_hours(h: float) -> str:
    m = max(0, round(h * 60))
    return "%dh%02dm" % (m // 60, m % 60)


def run(args, check: bool = False, timeout: float = 120, **kw) -> subprocess.CompletedProcess:
    """A program's result; a missing program (127) or a timeout (124) is a failed result, not an exception."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kw)
    except FileNotFoundError:
        p = subprocess.CompletedProcess(args, 127, "", "%s: not found" % args[0])
    except subprocess.TimeoutExpired:
        p = subprocess.CompletedProcess(args, 124, "", "timed out after %d s" % timeout)
    if check and p.returncode:
        raise RuntimeError("%s failed: %s" % (Path(str(args[0])).name, last_line(p.stderr or p.stdout)))
    return p


def git(*args: str, check: bool = True, timeout: float = 120, cwd=None) -> str:
    return run(["git", *args], check=check, timeout=timeout, cwd=str(cwd or cfg()["repo_dir"])).stdout


def tmux(*args: str) -> subprocess.CompletedProcess:
    return run(["tmux", *args], timeout=30)


def read_env_file(path: Path) -> dict:
    """KEY=value pairs from `export K='v'` lines (parsed, never run through a shell)."""
    env = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return env
    for line in lines:
        try:
            words = shlex.split(line)
        except ValueError:  # (a line that is not valid shell quoting is skipped, never fatal)
            continue
        for word in words:
            if "=" in word and word != "export":
                key, value = word.split("=", 1)
                env[key] = value
    return env


def worker_env(c: dict) -> dict:
    drop = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CONTAINER_API_KEY", "CONTAINER_ID")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PATH"] = "%s/.local/bin:/venv/main/bin:%s" % (ROOT, env.get("PATH", "/usr/bin:/bin"))
    env.update(
        IS_SANDBOX="1", DISABLE_AUTOUPDATER="1", CRAB_STUDENT_NAME=c["name"], CRAB_STUDENT_BRANCH=c["branch"]
    )
    env.setdefault("TERM", "xterm-256color")
    env.update(read_env_file(D / "claude.env"))
    env.update(read_env_file(D / "secrets.env"))
    return env


def report_path(c: dict) -> str:
    return "agent-runs/%s/REPORT.md" % c["name"]


# ---- setup ---------------------------------------------------------------------------------------


def cmd_setup() -> int:
    c, jobs = cfg(), {}
    if (D / "setup-done").exists():  # (a launch that was cut off after setup: the worker may be at work)
        spawn_watchdog()
        return out({"ok": True, "resumed": True})
    steps = (setup_environment, setup_repository, setup_settings, setup_claude, setup_start)
    for i, step in enumerate(steps, 1):
        label = STEPS[i - 1]
        print("step %d/5 %s" % (i, label), flush=True)
        try:
            step(c, jobs)
        except Exception as e:  # reported to the local side, which destroys the machine
            with open(str(D / "logs" / "setup.log"), "a") as f:
                f.write("step %d (%s) failed: %s\n" % (i, label, e))
            return out({"ok": False, "step": i, "label": label, "error": str(e)})
    return out({"ok": True})


def _script(path: Path, command: str) -> None:
    write(path, '#!/bin/sh\nexec %s %s "$@"\n' % (PY, command), 0o755)


def setup_environment(c: dict, jobs: dict) -> None:
    (D / "logs").mkdir(parents=True, exist_ok=True)
    write(PROFILE_D / "crab-student.sh", "export PATH=/root/.local/bin:/venv/main/bin:$PATH\n")
    write(ROOT / ".no_auto_tmux", "")
    write(ROOT / ".tmux.conf", TMUX_CONF.format(name=c["name"]))
    _script(BIN / "crab-status", "%s/cs.py declare" % D)
    _script(BIN / "crab-attach", "%s/cs.py attach" % D)
    for hook in ("pre-commit", "pre-push"):
        _script(D / "hooks" / hook, "%s/cs.py hook %s" % (D, hook))
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    run(["git", "config", "--global", "user.name", "crab-student worker (%s)" % c["name"]], check=True)
    run(["git", "config", "--global", "user.email", "%s@crab-student.invalid" % c["name"]], check=True)
    logs = open(str(D / "logs" / "install.log"), "a")  # noqa: SIM115 - the install jobs write to it
    if not c["fake_claude"] and not CLAUDE.exists():
        cmd = "curl -fsSL https://claude.ai/install.sh | bash || (sleep 5; curl -fsSL https://claude.ai/install.sh | bash)"
        jobs["Claude Code"] = subprocess.Popen(["bash", "-c", cmd], stdout=logs, stderr=subprocess.STDOUT)
    if not shutil.which("tmux"):
        cmd = "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tmux"
        jobs["tmux"] = subprocess.Popen(["bash", "-c", cmd], stdout=logs, stderr=subprocess.STDOUT)


def git_ssh() -> str:
    return (
        "ssh -i %s/deploy_key -o IdentitiesOnly=yes -o UserKnownHostsFile=%s/github_known_hosts "
        "-o StrictHostKeyChecking=yes" % (D, D)
    )


def setup_repository(c: dict, jobs: dict) -> None:
    repo = Path(c["repo_dir"])
    if not (repo / ".git").exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, GIT_SSH_COMMAND=git_ssh())
        base = ["git", "clone", "-q", "--branch", c["branch"], c["repo_url"], str(repo)]
        p = run([*base[:2], "--filter=blob:none", *base[2:]], env=env, timeout=900)
        if p.returncode:  # (a server without partial clones)
            shutil.rmtree(str(repo), ignore_errors=True)
            run(base, env=env, timeout=1800, check=True)
    git("config", "core.sshCommand", git_ssh(), cwd=repo)
    git("config", "core.hooksPath", str(D / "hooks"), cwd=repo)
    git("config", "remote.origin.push", "refs/heads/%s:refs/heads/%s" % (c["branch"], c["branch"]), cwd=repo)


def setup_settings(c: dict, jobs: dict) -> None:
    fill = {
        "name": c["name"],
        "branch": c["branch"],
        "repo_dir": c["repo_dir"],
        "report": report_path(c),
        "idle_minutes": str(c["idle_minutes"]),
        "secrets": ", ".join(c["secret_names"]) or "none",
    }
    rules = (D / "worker.md").read_text()
    for key, value in fill.items():
        rules = rules.replace("{{%s}}" % key, value)
    write(ROOT / ".claude" / "CLAUDE.md", rules)
    write(ROOT / ".claude" / "settings.json", json.dumps(SETTINGS, indent=2))
    state = read_json(ROOT / ".claude.json", {})
    state.update(hasCompletedOnboarding=True, bypassPermissionsModeAccepted=True)
    state.setdefault("projects", {}).setdefault(c["repo_dir"], {}).update(
        hasTrustDialogAccepted=True, hasCompletedProjectOnboarding=True
    )
    write(ROOT / ".claude.json", json.dumps(state, indent=2), 0o600)


def setup_claude(c: dict, jobs: dict) -> None:
    deadline = time.time() + 180
    for name, job in jobs.items():
        try:
            job.wait(max(1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            job.kill()
            raise RuntimeError("installing %s took over 3 minutes" % name) from None
    install_log = D / "logs" / "install.log"
    tail = last_line(install_log.read_text()) if install_log.exists() else "no output"
    if not shutil.which("tmux"):
        raise RuntimeError("tmux could not be installed (%s)" % tail)
    if c["fake_claude"]:
        return
    if run([str(CLAUDE), "--version"]).returncode:
        raise RuntimeError("Claude Code did not install (%s)" % tail)
    p = run(
        [str(CLAUDE), "-p", "Reply with the word ok", "--model", "haiku"],
        env=worker_env(c),
        timeout=120,
        cwd=c["repo_dir"],
    )
    if p.returncode:
        raise RuntimeError(
            "the Claude token was refused (%s): run crab-student init --claude-token, then "
            "crab-student start %s" % (last_line(p.stderr or p.stdout), c["name"])
        )


def setup_start(c: dict, jobs: dict) -> None:
    if not vast_credentials().get(
        "CONTAINER_API_KEY"
    ):  # (then neither the idle stop nor the limit would work)
        raise RuntimeError("this machine cannot stop itself: Vast gave it no CONTAINER_API_KEY")
    write(D / "setup-done", "", 0o600)
    spawn_watchdog()
    sid, deadline = session_id(c), time.time() + 90
    while time.time() < deadline:
        if tmux("has-session", "-t", "worker").returncode == 0 and transcript(sid):
            return
        time.sleep(2)
    screen = tmux("capture-pane", "-p", "-t", "worker").stdout.strip().splitlines()[-15:]
    raise RuntimeError("Claude Code did not start its conversation. Its screen shows:\n" + "\n".join(screen))


# ---- Claude Code in tmux -------------------------------------------------------------------------


def session_id(c: dict) -> str:
    replaced = D / "session_id"
    return replaced.read_text().strip() if replaced.exists() else c["session_id"]


def transcript(sid: str) -> bool:
    return bool(glob.glob(str(ROOT / ".claude" / "projects" / "*" / (sid + ".jsonl"))))


def claude_argv(c: dict, sid: str, first: bool, lost: bool) -> list:
    argv = [PY, str(D / "fake_claude.py")] if c["fake_claude"] else [str(CLAUDE)]
    argv += ["--dangerously-skip-permissions"] + (["--model", c["model"]] if c.get("model") else [])
    if transcript(sid):
        return [*argv, "--resume", sid, *([RESTART_NOTE] if first else [])]
    return [*argv, "--session-id", sid, LOST_PROMPT if lost else FIRST_PROMPT]


def cmd_claude() -> int:
    """Runs in the tmux pane: starts or resumes the worker's conversation, and again after it exits."""
    c, first, lost = cfg(), True, False
    while True:
        sid = session_id(c)
        resuming = transcript(sid)
        started = time.time()
        rc = subprocess.call(claude_argv(c, sid, first, lost), cwd=c["repo_dir"], env=worker_env(c))
        if resuming and rc and time.time() - started < 20:  # the saved conversation could not be resumed
            write(D / "session_id", str(uuid.uuid4()), 0o600)
            lost = True
            continue
        first = False
        print("\nClaude Code exited. Press Enter to start it again (same conversation).", flush=True)
        try:
            input()
        except EOFError:
            time.sleep(60)


def cmd_attach() -> int:
    spawn_watchdog()
    if tmux("has-session", "-t", "worker").returncode == 0:
        os.execvp("tmux", ["tmux", "-u", "attach", "-t", "worker"])  # (-u: UTF-8 even without a locale)
    print(
        "The worker's session is not running (see crab-student status %s). Opening a shell." % cfg()["name"]
    )
    os.execvp("bash", ["bash", "-l"])
    return 1


def type_to_worker(text: str) -> None:
    """Type a message into the worker's Claude Code as one bracketed paste, then Enter."""
    text = text.rstrip("\r\n")
    subprocess.run(["tmux", "load-buffer", "-b", "cs", "-"], input=text.encode(), check=True, timeout=30)
    subprocess.run(["tmux", "paste-buffer", "-p", "-d", "-b", "cs", "-t", "worker"], check=True, timeout=30)
    time.sleep(0.5)
    subprocess.run(["tmux", "send-keys", "-t", "worker", "Enter"], check=True, timeout=30)


# ---- the watchdog --------------------------------------------------------------------------------


class Probe:
    """What the watchdog measures (tests pass a fake)."""

    def transcript_bytes(self) -> int:
        return sum(
            os.path.getsize(p) for p in glob.glob(str(ROOT / ".claude" / "projects" / "*" / "*.jsonl"))
        )

    def cpu_seconds(self):
        """The container's own CPU time (its cgroup, never the host's /proc/stat)."""
        try:
            with open("/sys/fs/cgroup/cpu.stat") as f:
                for line in f:
                    if line.startswith("usage_usec"):
                        return int(line.split()[1]) / 1e6
        except (OSError, ValueError):
            pass
        try:
            return int(open("/sys/fs/cgroup/cpuacct/cpuacct.usage").read()) / 1e9
        except (OSError, ValueError):
            return None

    def gpu_busy(self) -> bool:
        try:
            p = run(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"], timeout=20
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return any(int(x) >= 10 for x in p.stdout.split() if x.strip().isdigit())


def read_worker() -> dict:
    w = read_json(D / "worker.json", {})
    return w if isinstance(w, dict) else {}


def tick(st: dict, c: dict, now: float, probe) -> list:
    """One watchdog step: update the state, return the actions (notify:<state>, nudge, warn, stop:<why>)."""
    actions = []
    dt = min(max(now - st["last_tick"], 0), 2 * TICK)
    st["last_tick"] = now
    st["used_seconds"] += dt
    size, cpu = probe.transcript_bytes(), probe.cpu_seconds()
    cpu_busy = (
        cpu is not None and st["cpu_seconds"] is not None and cpu - st["cpu_seconds"] >= CPU_BUSY_CORES * dt
    )
    if size != st["transcript_bytes"]:
        st["last_talk"], st["talk_nudged"] = now, False
    if size != st["transcript_bytes"] or cpu_busy or probe.gpu_busy():
        st["last_activity"], st["nudged_at"] = now, None
    st["transcript_bytes"], st["cpu_seconds"] = size, cpu
    state = read_worker().get("state")
    if state != st["notified_state"] and state in ("needs-you", "done", "failed"):
        actions.append("notify:" + state)
    st["notified_state"] = state
    idle, limit = now - st["last_activity"], c["limit_hours"] * 3600
    if state == "needs-you" and idle >= c["idle_minutes"] * 30 and st["nudged_at"] is None:
        actions.append("nudge")
        st["nudged_at"] = now
    # A worker that says "working" but has not said a word for a while has likely ended its turn with a
    # job still running (and nothing wakes an interactive session): remind it once per quiet stretch.
    if state == "working" and now - st["last_talk"] >= QUIET_MINUTES * 60 and not st["talk_nudged"]:
        actions.append("check-in")
        st["talk_nudged"] = True
    if st["used_seconds"] < limit - 600:
        st["warned_limit"] = False  # (re-armed after an extend)
    elif not st["warned_limit"]:
        actions.append("warn")
        st["warned_limit"] = True
    if st["used_seconds"] >= limit and now - st["boot"] >= GRACE:
        actions.append("stop:time-limit")
    elif state == "done" and idle >= DONE_MINUTES * 60:
        actions.append("stop:done")
    elif idle >= c["idle_minutes"] * 60:
        actions.append("stop:idle")
    return actions


def load_state() -> dict:
    st = read_json(D / "state.json", {})
    defaults = {
        "used_seconds": 0.0,
        "last_activity": 0.0,
        "last_tick": 0.0,
        "boot": None,
        "transcript_bytes": 0,
        "cpu_seconds": None,
        "stopping": None,
        "stop_reason": None,
        "notified_state": None,
        "nudged_at": None,
        "warned_limit": False,
        "last_talk": 0.0,
        "talk_nudged": False,
        "stop_sent_at": None,
    }
    return dict(defaults, **(st if isinstance(st, dict) else {}))


def save_state(st: dict) -> None:
    write(D / "state.json", json.dumps(st), 0o600)


def booted_at() -> float:
    path = D / "booted"
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        write(path, str(int(time.time())), 0o600)
        return float(path.read_text())


def spawn_watchdog() -> None:
    """Start the watchdog unless it runs (setup does this; status does it too, so a dead watchdog comes back)."""
    if not (D / "setup-done").exists() or not _lock_free(D / "watch.lock"):
        return
    with open(str(D / "logs" / "watch.log"), "a") as logs:
        subprocess.Popen(
            [PY, str(D / "cs.py"), "watch"],
            stdout=logs,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(D),
            start_new_session=True,
        )


def _lock(path: Path):
    import fcntl

    f = open(str(path), "a")  # noqa: SIM115 - held open: the lock lives as long as the file
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def _lock_free(path: Path) -> bool:
    f = _lock(path)
    if f:
        f.close()
    return f is not None


def ensure_worker_session(c: dict) -> None:
    if tmux("has-session", "-t", "worker").returncode:
        tmux(
            "new-session",
            "-d",
            "-s",
            "worker",
            "-x",
            "220",
            "-y",
            "50",
            "-c",
            c["repo_dir"],
            "%s %s/cs.py claude" % (PY, D),
        )


def cmd_watch() -> int:
    lock = _lock(D / "watch.lock")
    if not lock:
        return 0
    vast_env = vast_credentials()
    for key in ("CONTAINER_API_KEY", "CONTAINER_ID"):  # (the tmux server and the worker never see them)
        os.environ.pop(key, None)
    st, probe, now = load_state(), Probe(), time.time()
    if st["boot"] != booted_at():  # a new container start: its own idle clock and boot grace
        st.update(
            boot=booted_at(),
            last_activity=now,
            last_tick=now,
            last_talk=now,
            talk_nudged=False,
            stopping=None,
            stop_reason=None,
            stop_sent_at=None,
            nudged_at=None,
            transcript_bytes=probe.transcript_bytes(),
            cpu_seconds=probe.cpu_seconds(),
        )
    log("watchdog started")
    while True:
        try:
            c = cfg()
            write(D / "heartbeat", str(int(time.time())), 0o600)
            if st["stopping"]:
                vast_stop(vast_env)
            else:
                ensure_worker_session(c)
                for action in tick(st, c, time.time(), probe):
                    perform(action, st, c, vast_env)
            save_state(st)
        except Exception as e:  # the watchdog never dies of a bug (the dead-man switch is the net)
            log("tick failed: %r" % (e,))
        time.sleep(TICK)


def perform(action: str, st: dict, c: dict, vast_env: dict) -> None:
    w = read_worker()
    if action.startswith("notify:"):
        state = action.split(":", 1)[1]
        words = {"needs-you": "needs you", "done": "is done", "failed": "failed"}[state]
        notify(c, state, "%s %s: %s" % (c["name"], words, w.get("message", "")))
    elif action == "nudge":
        type_to_worker(NUDGE.format(minutes=c["idle_minutes"] // 2))
    elif action == "warn":
        type_to_worker(WARN.format(limit=fmt_hours(c["limit_hours"])))
    elif action == "check-in":
        type_to_worker(CHECK_IN.format(minutes=QUIET_MINUTES))
    elif action.startswith("stop:"):
        reason = action.split(":", 1)[1]
        why = {"idle": "idle for %d min" % c["idle_minutes"], "done": "finished %d min ago" % DONE_MINUTES,
               "time-limit": "time limit (%s) reached" % fmt_hours(c["limit_hours"])}[reason]  # fmt: skip
        st.update(stopping=reason, stop_reason=reason)
        save_state(st)
        log("stopping: " + why)
        notify(c, "stopped", "%s stopped: %s" % (c["name"], why))
        try:
            log("saved: %s" % json.dumps(save(reason)))
        except Exception as e:
            log("save failed: %r" % (e,))
        st["stop_sent_at"] = (
            time.time()
        )  # (after the save: from now on stopping it from outside loses nothing)
        save_state(st)
        vast_stop(vast_env)


def vast_credentials() -> dict:
    """CONTAINER_ID, CONTAINER_API_KEY (this machine's own Vast key) and CS_VAST_URL: from vast.env (written
    by onstart), else as the container's first process has them."""
    keys, first = ("CONTAINER_ID", "CONTAINER_API_KEY", "CS_VAST_URL"), {}
    try:
        with open("/proc/1/environ", "rb") as f:
            items = f.read().decode("utf-8", "replace").split(chr(0))
        first = {k: v for k, _, v in (item.partition("=") for item in items) if k in keys}
    except OSError:
        pass
    return dict(first, **read_env_file(D / "vast.env"))


def vast_stop(vast_env: dict) -> None:
    """Ask Vast to stop this machine with its own key (never destroy)."""
    url = "%s/api/v0/instances/%s/" % (
        vast_env.get("CS_VAST_URL", "https://console.vast.ai").rstrip("/"),
        vast_env.get("CONTAINER_ID", ""),
    )
    req = urllib.request.Request(
        url,
        data=json.dumps({"state": "stopped"}).encode(),
        method="PUT",
        headers={
            "Authorization": "Bearer " + vast_env.get("CONTAINER_API_KEY", ""),
            "Content-Type": "application/json",
            "User-Agent": "crab-student",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=30).close()
        log("asked Vast to stop the machine")
    except Exception as e:
        log("the stop call failed (it is tried again): %s" % (getattr(e, "code", None) or type(e).__name__))


def notify(c: dict, event: str, text: str) -> None:
    """Best effort: ntfy, Slack, Discord, or a generic JSON POST (the URL may hold a secret: never logged)."""
    url = c.get("notify_url") or ""
    if not url:
        return
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    headers, body = {"Content-Type": "application/json"}, None
    text = "crab-student: " + text
    if "ntfy" in host:
        tags = {"needs-you": "warning", "done": "white_check_mark", "failed": "x", "stopped": "stop_sign"}[
            event
        ]
        headers = {"Title": "crab-student: " + c["name"], "Tags": tags}
        body = text.encode()
    elif host == "hooks.slack.com":
        body = json.dumps({"text": text}).encode()
    elif host in ("discord.com", "discordapp.com") and "/api/webhooks/" in url:
        body = json.dumps({"content": text}).encode()
    else:
        body = json.dumps(
            {"worker": c["name"], "event": event, "message": text.split(": ", 2)[-1], "text": text}
        ).encode()
    headers["User-Agent"] = "crab-student"  # (some webhook hosts refuse Python's default)
    try:
        urllib.request.urlopen(
            urllib.request.Request(url, data=body, method="POST", headers=headers), timeout=10
        ).close()
    except Exception as e:
        log("notify failed: %s" % (getattr(e, "code", None) or type(e).__name__))


# ---- git: state, save, check ---------------------------------------------------------------------


def git_state(c: dict) -> dict:
    lines = git("status", "--porcelain=v2", "--branch", check=False).splitlines()
    head = next((x.split()[2][:7] for x in lines if x.startswith("# branch.oid")), "")
    branch = next((x.split()[2] for x in lines if x.startswith("# branch.head")), "")
    ab = next((x.split() for x in lines if x.startswith("# branch.ab")), None)
    unpushed = (
        int(ab[2].lstrip("+"))
        if ab
        else int(git("rev-list", "--count", "origin/%s..HEAD" % c["branch"], check=False).strip() or 0)
    )
    return {
        "branch": branch,
        "head": head,
        "dirty": sum(not x.startswith("#") for x in lines),
        "unpushed": unpushed,
    }


def untracked() -> list:
    return [f for f in git("ls-files", "--others", "--exclude-standard", "-z").split("\0") if f]


def save(reason: str) -> dict:
    """Commit and push what can safely be saved: tracked changes, and untracked files up to 10 MB each (at
    most 2,000 files and 100 MB in all: a forgotten dataset folder is no work to push), never during a merge
    or rebase, never on another branch, never past the pre-commit hook."""
    c = cfg()
    repo, branch = Path(c["repo_dir"]), c["branch"]
    res = {"committed": 0, "pushed": False, "skipped": [], "dirty": 0, "unpushed": 0, "error": None}
    if not (repo / ".git").exists():
        res["error"] = "there is no repository yet"
        return res
    busy = any((repo / ".git" / f).exists() for f in GIT_BUSY)
    if busy or git("symbolic-ref", "-q", "--short", "HEAD", check=False).strip() != branch:
        res["error"] = "a merge or rebase is in progress" if busy else "the repo is not on %s" % branch
    else:
        git("add", "-u")
        added, total, skipped = [], 0, []
        for f in untracked():  # (a broken symlink is no file: it is added as the link it is)
            size = (repo / f).stat().st_size if (repo / f).is_file() else 0
            if size > 10_000_000 or len(added) >= 2000 or total + size > 100_000_000:
                skipped.append(f)
            else:
                added.append(f)
                total += size
        res["skipped"] = skipped[:50]
        if added:  # (on stdin: thousands of paths would not fit on one command line)
            run(["git", "add", "--pathspec-from-file=-", "--pathspec-file-nul"], input="\0".join(added),
                check=True, cwd=str(repo))  # fmt: skip
        staged = [f for f in git("diff", "--cached", "--name-only", "-z").split("\0") if f]
        if staged:
            p = run(["git", "commit", "-q", "-m", "crab-student: autosave (%s)" % reason], cwd=str(repo))
            if p.returncode:
                git("reset", "-q")
                res["error"] = "not committed: " + last_line(p.stderr or p.stdout)
            else:
                res["committed"] = len(staged)
    p = run(["git", "push", "-q", "origin", "refs/heads/%s:refs/heads/%s" % (branch, branch)], cwd=str(repo))
    res["pushed"] = p.returncode == 0
    if p.returncode and not res["error"]:
        res["error"] = "push failed: " + last_line(p.stderr)
    res.update({k: v for k, v in git_state(c).items() if k in ("dirty", "unpushed")})
    return res


def cmd_check() -> int:
    """For delete: save, then report what would still be lost."""
    c = cfg()
    res = save("delete")
    repo, branch = Path(c["repo_dir"]), c["branch"]
    if (repo / ".git").exists():
        git("fetch", "-q", "origin", branch, check=False, timeout=60)
        res["unpushed"] = int(
            git("rev-list", "--count", "origin/%s..HEAD" % branch, check=False).strip() or 0
        )
    files = [p for p in OUTPUTS.rglob("*") if p.is_file()] if OUTPUTS.exists() else []
    res["outputs"] = {
        "files": len(files),
        "bytes": sum(p.stat().st_size for p in files),
        "sample": [str(p.relative_to(OUTPUTS)) for p in files[:5]],
    }
    tracked = set(git("ls-files", "-z", check=False).split("\0")) if (repo / ".git").exists() else set()
    large = []
    for top, dirs, names in os.walk(str(repo)):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            path = Path(top) / n
            rel = str(path.relative_to(repo)).replace(os.sep, "/")
            if rel not in tracked and path.is_file() and path.stat().st_size >= 100_000_000:
                large.append({"path": rel, "bytes": path.stat().st_size})
    res["large_files"] = large
    return out(res)


def cmd_status() -> int:
    c, st, now = cfg(), load_state(), time.time()
    spawn_watchdog()
    return out(
        {
            "name": c["name"],
            "worker": read_worker(),
            "idle_seconds": int(now - st["last_activity"]) if st["last_activity"] else 0,
            "used_hours": round(st["used_seconds"] / 3600, 3),
            "limit_hours": c["limit_hours"],
            "stopping": st["stopping"],
            "stop_reason": st["stop_reason"],
            "stop_sent_for": int(now - st["stop_sent_at"]) if st["stop_sent_at"] else None,
            "git": git_state(c),
        }
    )


def cmd_set_limit(hours: float) -> int:
    c = cfg()
    c["limit_hours"] = hours
    write(D / "config.json", json.dumps(c, indent=2), 0o600)
    return out({"limit_hours": hours, "used_hours": round(load_state()["used_seconds"] / 3600, 3)})


# ---- the worker's own commands -------------------------------------------------------------------


def cmd_declare(args: list) -> int:
    """crab-status: the worker's state and one-line message. "done" needs the report committed and pushed."""
    c = cfg()
    if not args:
        w, st = read_worker(), load_state()
        used = st["used_seconds"] / 3600
        print("status: %s - %s" % (w.get("state", "not set"), w.get("message", "")))
        print(
            "machine time: %s used of %s (%s left); the machine stops after %d min without activity"
            % (
                fmt_hours(used),
                fmt_hours(c["limit_hours"]),
                fmt_hours(c["limit_hours"] - used),
                c["idle_minutes"],
            )
        )
        return 0
    state, message = args[0], " ".join(" ".join(args[1:]).split())[:120]
    if state not in ("working", "needs-you", "done", "failed"):
        print('usage: crab-status working|needs-you|done|failed "<one-line message>"', file=sys.stderr)
        return 2
    if state == "done":
        problems = done_problems(c)
        if problems:
            print(
                "not done yet: %s. Commit and push (git push), then run crab-status done again."
                % "; ".join(problems)
            )
            return 1
    write(D / "worker.json", json.dumps({"state": state, "message": message, "at": int(time.time())}))
    print("status: %s - %s" % (state, message))
    return 0


def done_problems(c: dict) -> list:
    problems = []
    if run(["git", "ls-files", "--error-unmatch", report_path(c)], cwd=c["repo_dir"]).returncode:
        problems.append("%s is not committed" % report_path(c))
    dirty = [x[3:] for x in git("status", "--porcelain").splitlines() if x.strip()]
    if dirty:
        problems.append("%d file(s) are not committed (%s)" % (len(dirty), ", ".join(dirty[:3])))
    git("fetch", "-q", "origin", c["branch"], check=False, timeout=30)
    ahead = int(git("rev-list", "--count", "origin/%s..HEAD" % c["branch"], check=False).strip() or 0)
    if ahead:
        problems.append("%d commit(s) are not pushed" % ahead)
    return problems


def cmd_hook(name: str, args: list) -> int:
    c = cfg()
    if name == "pre-push":
        want = "refs/heads/%s" % c["branch"]
        for line in sys.stdin.read().splitlines():
            parts = line.split()
            if len(parts) == 4 and (parts[2] != want or set(parts[1]) == {"0"}):
                what = "delete " + c["branch"] if parts[2] == want else parts[2]
                print(
                    "crab-student: this worker may push only to %s, not %s." % (c["branch"], what),
                    file=sys.stderr,
                )
                return 1
        return 0
    secrets = dict(
        read_env_file(D / "secrets.env"),
        CLAUDE_CODE_OAUTH_TOKEN=read_env_file(D / "claude.env").get("CLAUDE_CODE_OAUTH_TOKEN", ""),
    )
    key_lines = (
        [x for x in (D / "deploy_key").read_text().splitlines() if x and not x.startswith("-")][:1]
        if (D / "deploy_key").exists()
        else []
    )
    if key_lines:
        secrets["the deploy key"] = key_lines[0]
    current = ""
    for line in git("diff", "--cached", "-U0", "--no-color").splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+"):
            for secret, value in secrets.items():
                if len(value) >= 8 and value in line:
                    print(
                        "crab-student: %s contains the value of secret %s. Use the environment variable instead."
                        % (current, secret),
                        file=sys.stderr,
                    )
                    return 1
    for f in [x for x in git("diff", "--cached", "--name-only", "--diff-filter=AM", "-z").split("\0") if x]:
        path = Path(c["repo_dir"]) / f
        if path.is_file() and path.stat().st_size > 50_000_000:
            print(
                "crab-student: %s is %.1f GB: keep big files out of git (add it to .gitignore; put outputs in "
                "/root/outputs)." % (f, path.stat().st_size / 1e9),
                file=sys.stderr,
            )
            return 1
    return 0


def main(argv: list) -> int:
    cmd, args = (argv[0] if argv else ""), argv[1:]
    handlers = {
        "setup": cmd_setup,
        "watch": cmd_watch,
        "claude": cmd_claude,
        "attach": cmd_attach,
        "status": cmd_status,
        "check": cmd_check,
        "send": lambda: (type_to_worker(sys.stdin.read()), out({"sent": True}))[1],
        "save": lambda: out(save(args[args.index("--reason") + 1] if "--reason" in args else "manual")),
        "set-limit": lambda: cmd_set_limit(float(args[0])),
        "declare": lambda: cmd_declare(args),
        "hook": lambda: cmd_hook(args[0], args[1:]),
    }
    if cmd not in handlers:
        print("usage: cs.py " + "|".join(handlers), file=sys.stderr)
        return 2
    try:
        return handlers[cmd]()
    except Exception as e:
        print(json.dumps({"error": str(e) or type(e).__name__}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
