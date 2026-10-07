#!/usr/bin/env python3
"""crab-student on the machine: setup, the watchdog, Claude Code in tmux, the hand-off from VS Code, and the
commands the local side runs over ssh (each prints one JSON object on its last line). Python 3.8+ standard
library only. What the agent reads never names crab-student: it has no context for the name."""

from __future__ import annotations

import glob
import json
import os
import platform
import re
import shlex
import shutil
import signal
import stat
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
BRIEF = ROOT / "BRIEF.md"  # (where the agent reads its brief, outside the tool's folder)
VENV = Path(os.environ.get("CS_VENV", "/venv/main"))  # the image's Python environment, when it has one
VSCODE_HOME = ROOT / ".vscode-server"  # (VS Code's server, its extensions and its machine settings)
VSCODE_DOWNLOAD = "https://update.code.visualstudio.com/commit:%s/%s/stable"
CLAUDE_EXTENSION = "anthropic.claude-code"
# What installing the dependencies leaves in the repo, ignored machine-wide so that neither the autosave nor
# the agent commits it (and agent-status done is not refused for it).
INSTALL_LEFTOVERS = (".venv/", "*.egg-info/", "__pycache__/")
TICK = float(os.environ.get("CS_TICK_SECONDS", "60"))
PY = sys.executable or "python3"
CPU_BUSY_CORES = 0.25
QUIET_MINUTES = 20  # a "working" agent silent this long gets a check-in (longer than its longest sleep)
GIT_BUSY = ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache"}
STEPS = ("environment", "repository", "dependencies", "Claude Code", "agent settings", "starting the agent")
DEPS_TIMEOUT = 1800  # (30 minutes, room for CUDA wheels on a slow host, within the dead-man switch's 45)
SESSION = "agent"  # the tmux session Claude Code runs in
# What the config's claude_settings may set (modelSettings: the effort Claude Code keeps for each model).
CLAUDE_KEYS = ("model", "effortLevel", "modelSettings", "fastMode")

MACHINE = "Automated message from the machine: "
FIRST_PROMPT = (
    "Your task is in %s and your rules for working on this machine are in ~/.claude/CLAUDE.md. Read both, "
    'run agent-status working "<first step>", then start.' % BRIEF
)
LOST = (
    " Your previous conversation could not be restored: read your report and git log to see how far you "
    "got, then continue."
)
RESTART_NOTE = MACHINE + (
    "Claude Code was restarted (the machine was started again, the host rebooted, or Claude Code crashed). "
    "Background jobs from before are gone. If you were in the middle of the task, check where things stand "
    '(git log, your logs, checkpoints) and continue. If you had finished, reply "ok" and wait.'
)
NUDGE = MACHINE + (
    "nobody has answered for {minutes} minutes. Choose what you think is best, write the choice and your "
    'reasons in your report under "Decisions", run agent-status working, and continue.'
)
CHECK_IN = MACHINE + (
    "you have been quiet for {minutes} minutes while your status says working. Check on your running jobs "
    "and carry on with the task; if you are finished, complete the report and run agent-status done."
)
HANDOFF_PROMPT = MACHINE + (
    "the user has handed this conversation over to you, to continue {how}. They are leaving VS Code: from "
    "now on you run in a terminal on this machine, where they can look in and type to you. Your rules for "
    "this are now in ~/.claude/CLAUDE.md: read them (the status line, the report, git), run agent-status "
    'working "<first step>", and continue with: {prompt}'
)
HOW = {
    "autonomous": "on your own, without asking them anything",
    "colleague": "on your own, asking them only about important or hard-to-reverse choices",
}
TMUX_CONF = (
    'set -g mouse on\nset -g history-limit 50000\nset -sg escape-time 10\nset -g default-terminal "screen-256color"\n'
    'set -g status-left "[{name}] "\nset -g status-left-length 40\nset -g status-right "detach: Ctrl-b d "\n'
)
# Every Claude Code on the machine (in tmux, in VS Code, the setup check) skips permission prompts: the
# machine is disposable, and the git hooks guard the repo. (As root, Claude Code refuses to skip them
# unless IS_SANDBOX=1.)
SETTINGS = {
    "env": {"DISABLE_AUTOUPDATER": "1", "IS_SANDBOX": "1"},
    "skipDangerousModePermissionPrompt": True,  # (the "Bypass Permissions mode" warning, accepted)
    "permissions": {
        "defaultMode": "bypassPermissions",
        "deny": [
            "Bash(git push --no-verify:*)",
            "Bash(git commit --no-verify:*)",
            "Bash(git commit -n:*)",
            "Bash(git config core.hooksPath:*)",
            "Bash(git -c core.hooksPath:*)",
            "Bash(git remote set-url:*)",
            "Bash(git remote add:*)",
            "Bash(git push --mirror:*)",
        ],
    },
}
# VS Code's settings for this machine (Remote-SSH reads them; nothing goes into the user's repo): the Claude
# extension starts without permission prompts too, and a terminal opens on the agent's tmux session (or a
# shell while Claude works in VS Code).
VSCODE_SETTINGS = {
    "claudeCode.initialPermissionMode": "bypassPermissions",
    "claudeCode.allowDangerouslySkipPermissions": True,
    "terminal.integrated.profiles.linux": {"agent": {"path": str(BIN / "agent-attach")}},
    "terminal.integrated.defaultProfile.linux": "agent",
}


# ---- small helpers -------------------------------------------------------------------------------


def cfg() -> dict:
    return json.loads((D / "config.json").read_text())


def write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))  # (the watchdog and a command may race)
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


def inherited(key: str) -> bool:
    """A variable Claude Code in tmux must not get: the machine's own Vast key, or a parent Claude Code's or VS
    Code's (a hand-off runs from the Claude Code in VS Code, and starts the tmux server)."""
    return key.startswith(("CONTAINER_", "CLAUDE_CODE_", "VSCODE_")) or key == "CLAUDECODE"


def agent_env(c: dict) -> dict:
    """Claude Code's environment in tmux, with the token, the task's secrets and the Python environment."""
    env = {k: v for k, v in os.environ.items() if not inherited(k)}
    env["PATH"] = "%s:%s" % (path_head(c), env.get("PATH", "/usr/bin:/bin"))
    env.pop("VIRTUAL_ENV", None)
    venv = active_venv(c)
    if venv:
        env["VIRTUAL_ENV"] = str(venv)
    env.update(IS_SANDBOX="1", DISABLE_AUTOUPDATER="1", AGENT_NAME=c["name"], AGENT_BRANCH=c["branch"])
    env.setdefault("TERM", "xterm-256color")
    env.update(read_env_file(D / "claude.env"))
    env.update(read_env_file(D / "secrets.env"))
    return env


def path_head(c: dict) -> str:
    """What goes before PATH: the repo's own .venv (once the install makes one; until then the shell skips
    it), the tools installed for root (Claude Code, uv), the image's Python environment."""
    return "%s/.venv/bin:%s/.local/bin:%s/bin" % (c["repo_dir"], ROOT, VENV)


def active_venv(c: dict):
    """The Python environment shells use: the repo's .venv once there is one, else the image's (or None)."""
    for venv in (Path(c["repo_dir"]) / ".venv", VENV):
        if (venv / "bin" / "python").exists():
            return venv
    return None


def python() -> str:
    """The Python that installs the repo's packages: the image's environment's, else the system's."""
    return str(VENV / "bin" / "python") if (VENV / "bin" / "python").exists() else "python3"


def report_path(c: dict) -> str:
    return "agent-runs/%s/REPORT.md" % c["name"]


def read_handoff() -> dict:
    """The hand-off from VS Code, once there was one: mode, from (the conversation forked), to (the session id
    it is forked into), prompt, at."""
    return read_json(D / "handoff.json", {})


def save_handoff(h: dict) -> None:
    write(D / "handoff.json", json.dumps(h), 0o600)


def machine_mode(c: dict) -> str:
    """The mode now: the launch's, until a hand-off switches an interactive agent to another."""
    return read_handoff().get("mode") or c["mode"]


def frontend(c: dict) -> str:
    """Where Claude works: in tmux, or in VS Code (an interactive agent not handed off)."""
    return "vscode" if machine_mode(c) == "interactive" else "tmux"


# ---- setup ---------------------------------------------------------------------------------------


def cmd_setup() -> int:
    _held = _lock(
        D / "setup.lock", wait=True
    )  # (a setup run again after a lost connection waits for the first)
    c, jobs = cfg(), {}
    if (D / "setup-done").exists():  # (a launch that was cut off after setup: the agent may be at work)
        spawn_watchdog()
        return out({"ok": True, "resumed": True})
    steps = (
        setup_environment,
        setup_repository,
        setup_dependencies,
        setup_claude,  # (before the settings: Claude Code's installer writes ~/.claude.json too)
        setup_settings,
        setup_start,
    )
    for i, step in enumerate(steps, 1):
        label = STEPS[i - 1]
        print("step %d/%d %s" % (i, len(steps), label), flush=True)
        # (each step is bounded: the dead-man switch waits for it)
        write(D / "heartbeat", str(int(time.time())), 0o600)
        try:
            step(c, jobs)
        except Exception as e:  # reported to the local side, which destroys the machine
            with open(str(D / "logs" / "setup.log"), "a") as f:
                f.write("step %d (%s) failed: %s\n" % (i, label, e))
            return out({"ok": False, "step": i, "label": label, "error": str(e)})
    return out({"ok": True, "warnings": deps_warnings() + vscode_server_ready(jobs)})


def vscode_server_ready(jobs: dict) -> list:
    """Wait for VS Code's server and the Claude extension to be in place: a warning when they are not."""
    job = jobs.get("VS Code")
    if job is None:
        return []
    try:
        if job.wait(600) == 0:
            return []
    except subprocess.TimeoutExpired:
        job.kill()
    tail = last_line((D / "logs" / "vscode.log").read_text(errors="replace"))
    return ["VS Code's server and its Claude extension could not be put on the machine ahead of time (%s): VS "
            "Code installs its server when you first open the agent, and the extension only if your "
            "remote.SSH.defaultExtensions lists %s." % (tail, CLAUDE_EXTENSION)]  # fmt: skip


def _script(path: Path, command: str) -> None:
    write(path, '#!/bin/sh\nexec %s %s "$@"\n' % (PY, command), 0o755)


def setup_environment(c: dict, jobs: dict) -> None:
    (D / "logs").mkdir(parents=True, exist_ok=True)
    write_profile(c)
    write(ROOT / ".no_auto_tmux", "")
    write(ROOT / ".tmux.conf", TMUX_CONF.format(name=c["name"]))
    _script(BIN / "agent-attach", "%s/cs.py attach" % D)
    _script(BIN / "agent-status", "%s/cs.py declare" % D)
    if machine_mode(c) == "interactive":
        _script(BIN / "agent-handoff", "%s/cs.py handoff" % D)
    for hook in ("pre-commit", "pre-push"):
        _script(D / "hooks" / hook, "%s/cs.py hook %s" % (D, hook))
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    run(["git", "config", "--global", "user.name", "%s (Claude Code agent)" % c["name"]], check=True)
    run(["git", "config", "--global", "user.email", "%s@agent.invalid" % c["name"]], check=True)
    ignore = ROOT / ".config" / "git" / "ignore"  # (git's own global ignore file)
    kept = ignore.read_text().splitlines() if ignore.exists() else []
    write(ignore, "\n".join(kept + [x for x in INSTALL_LEFTOVERS if x not in kept]) + "\n")
    logs = open(str(D / "logs" / "install.log"), "a")  # noqa: SIM115 - the install jobs write to it
    if not c["fake_claude"] and not CLAUDE.exists():
        cmd = "curl -fsSL https://claude.ai/install.sh | bash || (sleep 5; curl -fsSL https://claude.ai/install.sh | bash)"
        jobs["Claude Code"] = subprocess.Popen(["bash", "-c", cmd], stdout=logs, stderr=subprocess.STDOUT)
    if c.get("vscode_commit"):  # (an interactive agent's VS Code finds its server in place on the first open)
        with open(str(D / "logs" / "vscode.log"), "a") as vlog:
            cmd = vscode_server_command(c["vscode_commit"])
            jobs["VS Code"] = subprocess.Popen(["bash", "-c", cmd], stdout=vlog, stderr=subprocess.STDOUT)
    if not shutil.which("tmux"):
        cmd = "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -o DPkg::Lock::Timeout=300 tmux"
        jobs["tmux"] = subprocess.Popen(["bash", "-c", cmd], stdout=logs, stderr=subprocess.STDOUT)


def write_profile(c: dict) -> None:
    """The login profile (VS Code's terminals and tmux's shells are login shells): the tools, the token and the
    task's secrets (which stay in their 0600 files), the Python environment, and interactive shells starting in
    the repo. ~/.bashrc runs it again last, since an image's own .bashrc may activate its environment."""
    repo, head = shlex.quote(c["repo_dir"]), path_head(c)
    lines = [
        "export IS_SANDBOX=1 DISABLE_AUTOUPDATER=1",
        'for f in %s/claude.env %s/secrets.env; do [ -r "$f" ] && . "$f"; done' % (D, D),
        'case "$PATH" in "%s:"*) ;; *) export PATH="%s:$PATH" ;; esac' % (head, head),
        "if [ -x %s/.venv/bin/python ]; then export VIRTUAL_ENV=%s/.venv" % (repo, repo),
        "elif [ -x %s/bin/python ]; then export VIRTUAL_ENV=%s; fi" % (VENV, VENV),
        'case $- in *i*) if [ "$PWD" = "$HOME" ] && [ -d %s ]; then cd %s; fi ;; esac' % (repo, repo),
    ]
    write(PROFILE_D / "agent.sh", "\n".join(lines) + "\n")
    profile = PROFILE_D / "agent.sh"
    bashrc, line = ROOT / ".bashrc", "[ -r %s ] && . %s" % (profile, profile)
    text = bashrc.read_text() if bashrc.exists() else ""
    if line not in text:
        write(bashrc, text + ("\n" if text and not text.endswith("\n") else "") + line + "\n")


def vscode_server_command(commit: str) -> str:
    """Download the server of the user's VS Code (`commit`) into where Remote-SSH looks for it, and install the
    Claude extension with the server's own CLI."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return "echo 'not a VS Code commit'; exit 1"
    arch = "arm64" if platform.machine() in ("aarch64", "arm64") else "x64"
    server = shlex.quote(str(VSCODE_HOME / "cli" / "servers" / ("Stable-" + commit) / "server"))
    return (
        "set -o pipefail; mkdir -p %s && ([ -x %s/bin/code-server ] || curl -fsSL --retry 3 %s | tar -xz -C %s "
        "--strip-components=1) && %s/bin/code-server --install-extension %s --extensions-dir %s"
        % (server, server, VSCODE_DOWNLOAD % (commit, "server-linux-" + arch), server, server, CLAUDE_EXTENSION,
           shlex.quote(str(VSCODE_HOME / "extensions")))
    )  # fmt: skip


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
    """The agent's rules (an interactive agent gets the /handoff command instead: the user drives it), Claude
    Code's and VS Code's settings for the machine, and Claude Code's onboarding and trust flags."""
    mode = machine_mode(c)
    move(D / "BRIEF.md", BRIEF)  # (moved, not copied: one brief on the machine)
    if mode == "interactive":
        move(D / "handoff.md", ROOT / ".claude" / "commands" / "handoff.md")
    else:
        (D / "handoff.md").unlink(missing_ok=True)
        write_rules(c, mode)
    write_claude_settings(c)
    write_vscode_settings(c)
    state = read_json(ROOT / ".claude.json", {})
    state.update(hasCompletedOnboarding=True)
    state.setdefault("projects", {}).setdefault(c["repo_dir"], {}).update(
        hasTrustDialogAccepted=True, hasCompletedProjectOnboarding=True
    )
    write(ROOT / ".claude.json", json.dumps(state, indent=2), 0o600)


def move(src: Path, dest: Path) -> None:
    """An uploaded file to where it is used (a setup that runs again finds it there already)."""
    if src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(src), str(dest))


def write_vscode_settings(c: dict) -> None:
    """VS Code's settings for this machine, with the Python it should use: the repo's .venv when the install
    made one, else the image's (the user's own interpreter path, from their computer, means nothing here)."""
    path = VSCODE_HOME / "data" / "Machine" / "settings.json"
    venv = active_venv(c)
    settings = dict(read_json(path, {}), **VSCODE_SETTINGS)
    settings["python.defaultInterpreterPath"] = (
        str(venv / "bin" / "python") if venv else shutil.which("python3") or "python3"
    )
    write(path, json.dumps(settings, indent=2))


def write_rules(c: dict, mode: str) -> None:
    """~/.claude/CLAUDE.md: the rules every agent in tmux follows, with its mode's section."""
    fill = {
        "mode_rules": (D / ("rules-%s.md" % mode)).read_text().strip(),
        "name": c["name"],
        "branch": c["branch"],
        "repo_dir": c["repo_dir"],
        "brief": str(BRIEF),
        "report": report_path(c),
        "idle_minutes": str(c["idle_minutes"]),
        "secrets": ", ".join(c["secret_names"]) or "none",
        "dependencies": deps_rule(),
    }
    rules = (D / "rules.md").read_text()
    for key, value in fill.items():
        rules = rules.replace("{{%s}}" % key, value)
    write(ROOT / ".claude" / "CLAUDE.md", rules)


def write_claude_settings(c: dict) -> None:
    """~/.claude/settings.json (0600): the base settings, the model, effort (also per model) and fast mode the
    launch chose, and the token and task secrets in `env`, so every Claude Code here has them however it was
    started (the one VS Code starts too)."""
    settings = json.loads(json.dumps(SETTINGS))
    settings.update((k, v) for k, v in c["claude_settings"].items() if k in CLAUDE_KEYS)
    if settings.get("fastMode"):
        # (Claude Code's own availability check turns fast mode off for the long-lived token; the API accepts
        # it, and falls back to standard speed where fast mode is not allowed)
        settings["env"]["CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK"] = "1"
    settings["env"].update(read_env_file(D / "claude.env"))
    settings["env"].update(read_env_file(D / "secrets.env"))
    write(ROOT / ".claude" / "settings.json", json.dumps(settings, indent=2), 0o600)


def setup_claude(c: dict, jobs: dict) -> None:
    deadline = time.time() + 180
    for name, job in jobs.items():
        if name == "VS Code":  # (not needed by the agent: setup waits for it last, and never fails for it)
            continue
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
        env=agent_env(c),
        timeout=120,
        cwd=c["repo_dir"],
    )
    if p.returncode:
        raise RuntimeError(
            "the Claude token was refused (%s): run crab-student init --claude-token, then "
            "crab-student start %s" % (last_line(p.stderr or p.stdout), c["name"])
        )


def setup_start(c: dict, jobs: dict) -> None:
    if not vast_credentials().get("CONTAINER_API_KEY"):  # (then the idle stop would not work)
        raise RuntimeError("this machine cannot stop itself: Vast gave it no CONTAINER_API_KEY")
    write(D / "setup-done", "", 0o600)
    spawn_watchdog()
    if frontend(c) == "tmux" and not agent_started(c, 90):
        screen = tmux("capture-pane", "-p", "-t", SESSION).stdout.strip().splitlines()[-15:]
        raise RuntimeError(
            "Claude Code did not start its conversation. Its screen shows:\n" + "\n".join(screen)
        )


# ---- the repo's dependencies ---------------------------------------------------------------------


def deps_command(c: dict) -> str:
    """How the repo's dependencies are installed: the launch's --setup, else what the repo uses (the first that
    fits: uv, a pyproject.toml project, requirements files, setup.py); "" for nothing."""
    given = c.get("setup") or ""
    if given:
        return "" if given == "none" else given
    repo, py = Path(c["repo_dir"]), shlex.quote(python())
    try:
        pyproject = (repo / "pyproject.toml").read_text()
    except OSError:
        pyproject = ""
    if (repo / "uv.lock").exists() or re.search(r"(?m)^\[tool\.uv[\].]", pyproject):
        return "(command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh) && uv sync"
    if re.search(r"(?m)^\[project\]", pyproject):
        return "%s -m pip install -e ." % py
    reqs = sorted(p.name for p in repo.glob("requirements*.txt"))
    if reqs:
        return " && ".join("%s -m pip install -r %s" % (py, shlex.quote(r)) for r in reqs)
    if (repo / "setup.py").exists():
        return "%s -m pip install -e ." % py
    return ""


def setup_dependencies(c: dict, jobs: dict) -> None:
    """Install the repo's dependencies, while the installs the environment step started go on. A failed install
    never fails setup: setup's result warns of it, and the agent's rules say so."""
    command = deps_command(c)
    if not command:
        write(D / "deps.json", "{}")
        return
    env = dict(agent_env(c), PIP_ROOT_USER_ACTION="ignore", PIP_PROGRESS_BAR="off", TERM="dumb")
    env["PIP_BREAK_SYSTEM_PACKAGES"] = "1"  # (pip with the system's python3, on an image without /venv/main)
    env.pop("VIRTUAL_ENV", None)  # (uv sync would say it is not the project's environment)
    with open(str(D / "logs" / "deps.log"), "a") as logs:
        job = subprocess.Popen(["bash", "-c", command], cwd=c["repo_dir"], env=env, stdin=subprocess.DEVNULL,
                               stdout=logs, stderr=subprocess.STDOUT, start_new_session=True)  # fmt: skip
        try:
            code = job.wait(DEPS_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(job.pid, signal.SIGKILL)  # (the whole install, not only its shell)
            code = job.wait()
            logs.write("\nThe install was stopped after %d minutes.\n" % (DEPS_TIMEOUT // 60))
    write(D / "deps.json", json.dumps({"command": command, "exit_code": code}))


def read_deps() -> dict:
    """The dependency install at setup: command and exit_code (no command when there was nothing to install)."""
    return read_json(D / "deps.json", {})


def deps_log_tail(n: int) -> list:
    try:
        lines = [x.rstrip() for x in (D / "logs" / "deps.log").read_text(errors="replace").splitlines()]
    except OSError:
        return []
    return [x for x in lines if x.strip()][-n:]


def deps_warnings() -> list:
    """Setup's warning when the dependency install failed (the machine is set up all the same)."""
    d = read_deps()
    if not d.get("command") or d.get("exit_code") == 0:
        return []
    tail = (deps_log_tail(1) or ["no output"])[0].strip()
    return [
        "Installing the project's dependencies (%s) failed with exit code %s (%s). The machine was set up anyway; "
        "the install's log is %s there." % (d["command"], d["exit_code"], tail, D / "logs" / "deps.log")
    ]


def deps_rule() -> str:
    """The rules' line about the dependencies (with the end of a failed install's output, never its log's path)."""
    d = read_deps()
    command, code = d.get("command"), d.get("exit_code")
    if not command:
        return (
            "Only Claude Code and tmux were installed for you. Set up what the task needs the way the repo does it "
            "(`requirements.txt`, `pyproject.toml`, `uv.lock`, `environment.yml`, its README)."
        )
    if code == 0:
        return "The repo's dependencies were installed when the machine was set up (`%s`)." % command
    tail = "\n".join("  " + x for x in deps_log_tail(12))
    return (
        "Installing the repo's dependencies when the machine was set up (`%s`) failed with exit code %s. Fix that "
        "first, unless it has been fixed since. The end of its output:\n\n  ```\n%s\n  ```"
        % (command, code, tail)
    )


# ---- Claude Code in tmux -------------------------------------------------------------------------


def transcript(sid: str) -> bool:
    return bool(glob.glob(str(ROOT / ".claude" / "projects" / "*" / (sid + ".jsonl"))))


def session_id(c: dict) -> str:
    """The conversation Claude Code in tmux resumes (a hand-off, or a conversation that could not be resumed,
    gives it a new one)."""
    path = D / "session_id"
    return path.read_text().strip() if path.exists() else c["session_id"]


def fork_from(h: dict, sid: str):
    """The VS Code conversation to fork into tmux as `sid`: only as the session id the hand-off chose, so once
    that id is given up for a new one, never again."""
    return h["from"] if h.get("to") == sid and transcript(h["from"]) else None


def session_running() -> bool:
    return tmux("has-session", "-t", SESSION).returncode == 0


def wait_until(condition, seconds: float) -> bool:
    """Whether `condition()` holds, waiting up to `seconds` for it."""
    deadline = time.time() + seconds
    while not condition():
        if time.time() >= deadline:
            return False
        time.sleep(1)
    return True


def agent_started(c: dict, seconds: float) -> bool:
    """Whether Claude Code runs in tmux with its conversation saved, waiting up to `seconds` for it."""
    return wait_until(lambda: session_running() and transcript(session_id(c)), seconds)


def first_prompt(lost: bool) -> str:
    """A new conversation's prompt (after a hand-off, with what the user asked for then)."""
    asked = read_handoff().get("prompt")
    words = " When they handed the task over, the user asked you to continue with: " + asked if asked else ""
    return FIRST_PROMPT + (LOST if lost else "") + words


def claude_argv(c: dict, sid: str, first: bool, lost: bool) -> list:
    """A saved conversation is resumed; after a hand-off, the VS Code one is forked under `sid`; else a new one
    starts."""
    argv = [PY, str(D / "fake_claude.py")] if c["fake_claude"] else [str(CLAUDE)]
    argv += ["--dangerously-skip-permissions"]
    if transcript(sid):
        return [*argv, "--resume", sid, *([RESTART_NOTE] if first else [])]
    h = read_handoff()
    if fork_from(h, sid):
        prompt = HANDOFF_PROMPT.format(how=HOW[h["mode"]], prompt=h["prompt"])
        return [*argv, "--resume", h["from"], "--fork-session", "--session-id", sid, prompt]
    return [*argv, "--session-id", sid, first_prompt(lost)]


def cmd_claude() -> int:
    """Runs in the tmux pane: starts, resumes or forks the agent's conversation, and again after it exits."""
    c, first, lost = cfg(), True, False
    while True:
        sid = session_id(c)
        resuming = transcript(sid)
        forking = not resuming and bool(fork_from(read_handoff(), sid))
        started = time.time()
        rc = subprocess.call(claude_argv(c, sid, first, lost), cwd=c["repo_dir"], env=agent_env(c))
        if (resuming or forking) and rc and time.time() - started < 20:  # it could not be resumed or forked
            write(D / "session_id", str(uuid.uuid4()), 0o600)  # (a new conversation starts from the brief)
            lost = True
            continue
        first = False
        print("\nClaude Code exited. Press Enter to start it again (same conversation).", flush=True)
        try:
            input()
        except EOFError:
            time.sleep(60)


def cmd_attach() -> int:
    """agent-attach: the agent's tmux session (waiting a little for one a hand-off is starting), else a shell
    (VS Code's terminals run this too)."""
    spawn_watchdog()
    c = cfg()
    if frontend(c) == "tmux" and wait_until(session_running, 30):
        os.execvp("tmux", ["tmux", "-u", "attach", "-t", SESSION])  # (-u: UTF-8 even without a locale)
    if frontend(c) == "tmux":
        print("The agent's session is not running (see crab-student status %s). Opening a shell." % c["name"])
    elif os.environ.get("TERM_PROGRAM") != "vscode":  # (in VS Code itself, a plain shell says enough)
        print(
            "%s works in VS Code, not in tmux: open it with crab-student open %s --vscode (or type /handoff "
            "in its Claude chat to leave it running here). Opening a shell." % (c["name"], c["name"])
        )
    os.execvp("bash", ["bash", "-l"])
    return 1


def type_to_agent(text: str) -> None:
    """Type a message into the agent's Claude Code as one bracketed paste, then Enter."""
    text = text.rstrip("\r\n")
    subprocess.run(["tmux", "load-buffer", "-b", "cs", "-"], input=text.encode(), check=True, timeout=30)
    subprocess.run(["tmux", "paste-buffer", "-p", "-d", "-b", "cs", "-t", SESSION], check=True, timeout=30)
    time.sleep(0.5)
    subprocess.run(["tmux", "send-keys", "-t", SESSION, "Enter"], check=True, timeout=30)


# ---- the watchdog --------------------------------------------------------------------------------


class Probe:
    """What the watchdog measures (tests pass a fake)."""

    def transcript_bytes(self) -> int:
        """The size of every conversation file, subagents' too: Claude in tmux or in VS Code at work."""
        pattern = str(ROOT / ".claude" / "projects" / "**" / "*.jsonl")
        return sum(os.path.getsize(p) for p in glob.glob(pattern, recursive=True))

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


def read_agent() -> dict:
    return read_json(D / "agent.json", {})


def tick(st: dict, c: dict, now: float, probe) -> list:
    """One watchdog step: update the state, return the actions (notify:<state>, nudge, check-in, stop:<why>).
    The one stop rule: `idle_minutes` with no CPU or GPU work and no conversation, done or not."""
    actions = []
    dt = min(max(now - st["last_tick"], 0), 2 * TICK)
    st["last_tick"] = now
    st["used_seconds"] += dt
    size, cpu, agent = probe.transcript_bytes(), probe.cpu_seconds(), read_agent()
    cpu_busy = (
        cpu is not None and st["cpu_seconds"] is not None and cpu - st["cpu_seconds"] >= CPU_BUSY_CORES * dt
    )
    work = cpu_busy or probe.gpu_busy()
    if size != st["transcript_bytes"]:
        st["last_talk"] = now
    if size != st["transcript_bytes"] or work:
        st["last_activity"] = now
    # The reminders come again only once the agent itself did something (work, or a status update): they
    # grow the conversation too, so an agent that cannot answer (a usage limit, a revoked token) would
    # otherwise be reminded, and its machine kept up, forever.
    if work or agent.get("at") != st["agent_at"]:
        st["nudged_at"], st["talk_nudged"] = None, False
    st["transcript_bytes"], st["cpu_seconds"], st["agent_at"] = size, cpu, agent.get("at")
    mode, state = machine_mode(c), agent.get("state")
    if state != st["notified_state"] and state in ("needs-you", "done", "failed"):
        actions.append("notify:" + state)
    st["notified_state"] = state
    idle = now - st["last_activity"]
    asking = mode == "colleague" and state == "needs-you"  # (only a colleague asks; nobody answers others)
    if asking and idle >= c["idle_minutes"] * 30 and st["nudged_at"] is None:
        actions.append("nudge")
        st["nudged_at"] = now
    # An agent that says "working" but has not said a word for a while has likely ended its turn with a
    # job still running (and nothing prompts an interactive session): remind it.
    quiet = now - st["last_talk"] >= QUIET_MINUTES * 60
    if mode != "interactive" and state == "working" and quiet and not st["talk_nudged"]:
        actions.append("check-in")
        st["last_talk"], st["talk_nudged"] = now, True  # (the check-in is talk: a new quiet stretch begins)
    if idle >= c["idle_minutes"] * 60:
        actions.append("stop:done" if state == "done" else "stop:idle")
    return actions


def load_state() -> dict:
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
        "last_talk": 0.0,
        "talk_nudged": False,
        "agent_at": None,
        "stop_sent_at": None,
    }
    return dict(defaults, **read_json(D / "state.json", {}))


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
    if (D / "setup-done").exists() and _lock_free(D / "watch.lock"):
        spawn("watch", "watch.log")


def spawn(command: str, logname: str) -> None:
    """`cs.py <command>` in the background, in a session of its own (it outlives the ssh call that started it),
    its output going to logs/<logname>."""
    with open(str(D / "logs" / logname), "a") as logs:
        subprocess.Popen(
            [PY, str(D / "cs.py"), command],
            stdout=logs,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(D),
            start_new_session=True,
        )


def _lock(path: Path, wait: bool = False):
    import fcntl

    f = open(str(path), "a")  # noqa: SIM115 - held open: the lock lives as long as the file
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        return f
    except OSError:
        f.close()
        return None


def _lock_free(path: Path) -> bool:
    f = _lock(path)
    if f:
        f.close()
    return f is not None


def ensure_agent_session(c: dict) -> None:
    if not session_running():
        tmux(
            "new-session",
            "-d",
            "-s",
            SESSION,
            "-x",
            "220",
            "-y",
            "50",
            "-c",
            c["repo_dir"],
            "%s %s/cs.py claude" % (PY, D),
        )


def drop_inherited() -> None:
    """Remove from this process's environment what a tmux server it starts (and so the agent) must never see."""
    for key in [k for k in os.environ if inherited(k)]:
        os.environ.pop(key)


def cmd_watch() -> int:
    lock = _lock(D / "watch.lock")
    if not lock:
        return 0
    vast_env = vast_credentials()
    drop_inherited()
    st, probe, now = load_state(), Probe(), time.time()
    if st["boot"] != booted_at():  # a new container start: its own idle clock and machine time
        st.update(
            boot=booted_at(),
            used_seconds=0.0,
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
            if st["stopping"]:
                vast_stop(vast_env)
            else:
                if frontend(c) == "tmux":
                    ensure_agent_session(c)
                for action in tick(st, c, time.time(), probe):
                    perform(action, st, c, vast_env)
            save_state(st)
            # (only a tick that did its work: one failing every minute leaves the dead-man switch to stop it)
            write(D / "heartbeat", str(int(time.time())), 0o600)
        except Exception as e:  # the watchdog never dies of a bug (the dead-man switch is the net)
            log("tick failed: %r" % (e,))
        time.sleep(TICK)


def perform(action: str, st: dict, c: dict, vast_env: dict) -> None:
    a = read_agent()
    if action.startswith("notify:"):
        state = action.split(":", 1)[1]
        words = {"needs-you": "needs you", "done": "is done", "failed": "failed"}[state]
        notify(c, state, "%s %s: %s" % (c["name"], words, a.get("message", "")))
    elif action == "nudge":
        type_to_agent(NUDGE.format(minutes=c["idle_minutes"] // 2))
    elif action == "check-in":
        type_to_agent(CHECK_IN.format(minutes=QUIET_MINUTES))
    elif action.startswith("stop:"):
        reason = action.split(":", 1)[1]
        why = {"idle": "idle for %d min", "done": "done, and idle for %d min"}[reason] % c["idle_minutes"]
        st.update(stopping=reason, stop_reason=reason)
        save_state(st)
        log("stopping: " + why)
        notify(c, "stopped", "%s stopped: %s" % (c["name"], why))
        try:
            log("saved: %s" % json.dumps(save(reason, final=True)))
        except Exception as e:
            log("save failed: %r" % (e,))
        st["stop_sent_at"] = time.time()  # (after the save: from now on a stop from outside loses nothing)
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
            {"agent": c["name"], "event": event, "message": text.split(": ", 2)[-1], "text": text}
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


def uncommitted() -> list:
    """What a commit now would leave out: unstaged changes to tracked files, and untracked files."""
    return [f for f in git("diff", "--name-only", "-z").split("\0") if f] + untracked()


def files_under(top: Path, skip=()):
    """(path relative to top, size) of each file under top, but in folders named in `skip`. (Jobs may still be
    writing: a file that vanishes on the way is left out.)"""
    for folder, dirs, names in os.walk(str(top)):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in names:
            path = Path(folder) / name
            try:
                info = path.stat()
            except OSError:  # (gone meanwhile, or a broken symlink)
                continue
            if stat.S_ISREG(info.st_mode):
                yield path.relative_to(top).as_posix(), info.st_size


def left_behind(repo: Path) -> dict:
    """What git will not save: the files in /root/outputs, and files of 100 MB+ in the repo that git does not
    track (ignored ones too)."""
    outputs = dict(files_under(OUTPUTS))
    tracked = set(git("ls-files", "-z", check=False).split("\0")) if (repo / ".git").exists() else set()
    large = [
        {"path": f, "bytes": n}
        for f, n in files_under(repo, SKIP_DIRS)
        if n >= 100_000_000 and f not in tracked
    ]
    return {
        "outputs": {"files": len(outputs), "bytes": sum(outputs.values()), "sample": list(outputs)[:5]},
        "large_files": large,
    }


def restore(repo: Path, path: str) -> None:
    """The file as HEAD has it, or no file if HEAD has none."""
    if run(["git", "checkout", "-q", "HEAD", "--", path], cwd=str(repo)).returncode:
        (repo / path).unlink(missing_ok=True)


def stop_note_path(c: dict) -> str:
    return "agent-runs/%s/stopped.json" % c["name"]


def stop_note(c: dict, reason: str) -> dict:
    """What the stopped machine will hold that GitHub does not, once the staged changes are committed (so the
    local side can delete a stopped agent without starting it again)."""
    left = [f for f in uncommitted() if f != stop_note_path(c)]
    behind = left_behind(Path(c["repo_dir"]))
    return {
        "stopped_at": int(time.time()),
        "reason": reason,
        "outputs": behind["outputs"],
        "large_files": behind["large_files"],
        "not_committed": left[:50],
        "not_committed_count": len(left),
    }


def save(reason: str, final: bool = False) -> dict:
    """Commit and push what can safely be saved: tracked changes, and untracked files up to 10 MB each (at
    most 2,000 files and 100 MB in all: a forgotten dataset folder is no work to push), never during a merge
    or rebase, never on another branch, never past the pre-commit hook. A final save (the machine is about to
    stop) puts the stop note in the same commit; `note` is the note, `note_committed` whether it went in."""
    c = cfg()
    repo, branch, note_path = Path(c["repo_dir"]), c["branch"], stop_note_path(c)
    res = {"committed": 0, "pushed": False, "skipped": [], "dirty": 0, "unpushed": 0, "error": None,
           "note": None, "note_committed": False}  # fmt: skip
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
        if final:
            res["note"] = stop_note(c, reason)
            write(repo / note_path, json.dumps(res["note"], indent=2) + "\n")
            git("add", "-f", "--", note_path)  # (-f: even where the repo's .gitignore would leave it out)
        staged = [f for f in git("diff", "--cached", "--name-only", "-z").split("\0") if f]
        if staged:
            p = run(["git", "commit", "-q", "-m", "Autosave (%s)" % reason], cwd=str(repo))
            if p.returncode:
                git("reset", "-q")
                res["error"] = "not committed: " + last_line(p.stderr or p.stdout)
                if final:  # (left in the work tree, the note would go into a later commit, stale)
                    restore(repo, note_path)
            else:
                res["committed"] = len([f for f in staged if f != note_path])
                res["note_committed"] = final
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
    res.update(left_behind(repo))
    return out(res)


def cmd_save(args: list) -> int:
    """save [--reason R] [--final]: --final (the machine is about to stop) adds the stop note."""
    reason = args[args.index("--reason") + 1] if "--reason" in args else "manual"
    return out(save(reason, final="--final" in args))


def cmd_status() -> int:
    c, st, now = cfg(), load_state(), time.time()
    spawn_watchdog()
    return out(
        {
            "name": c["name"],
            "mode": machine_mode(c),
            "frontend": frontend(c),
            "agent": read_agent(),
            "idle_seconds": int(now - st["last_activity"]) if st["last_activity"] else 0,
            "used_hours": round(st["used_seconds"] / 3600, 3),
            "stopping": st["stopping"],
            "stop_reason": st["stop_reason"],
            "stop_sent_for": int(now - st["stop_sent_at"]) if st["stop_sent_at"] else None,
            "git": git_state(c),
        }
    )


def cmd_send(text: str) -> int:
    c = cfg()
    if frontend(c) == "vscode":
        raise RuntimeError(
            "it works in VS Code, not in tmux: type to it there (crab-student open %s --vscode)" % c["name"]
        )
    type_to_agent(text)
    return out({"sent": True})


# ---- the agent's own commands --------------------------------------------------------------------


def cmd_declare(args: list) -> int:
    """agent-status: the agent's state and one-line message. "done" needs the report committed and pushed."""
    c = cfg()
    if not args:
        a, st = read_agent(), load_state()
        if machine_mode(c) != "interactive":
            print("status: %s - %s" % (a.get("state", "not set"), a.get("message", "")))
        print(
            "the machine has run for %s since it was started; it stops after %d min without activity"
            % (fmt_hours(st["used_seconds"] / 3600), c["idle_minutes"])
        )
        return 0
    if machine_mode(c) == "interactive":
        print(
            "This session is driven by the user, so it has no status to set (agent-status alone shows the machine)."
        )
        return 1
    state, message = args[0], " ".join(" ".join(args[1:]).split())[:120]
    if state not in ("working", "needs-you", "done", "failed"):
        print('usage: agent-status working|needs-you|done|failed "<one-line message>"', file=sys.stderr)
        return 2
    if state == "needs-you" and machine_mode(c) == "autonomous":
        print(
            "Nobody will answer: the user asked for this task to be done without them. Choose what they would "
            'most likely want, write the choice and your reasons under "Decisions" in your report, and continue.'
        )
        return 1
    if state == "done":
        problems = done_problems(c)
        if problems:
            print(
                "not done yet: %s. Commit and push (git push), then run agent-status done again."
                % "; ".join(problems)
            )
            return 1
    write(D / "agent.json", json.dumps({"state": state, "message": message, "at": int(time.time())}))
    print("status: %s - %s" % (state, message))
    return 0


def cmd_handoff(args: list) -> int:
    """agent-handoff [--session=ID] [autonomous|colleague] ["what to continue with"]: the conversation in VS
    Code goes on in tmux. It writes that mode's rules, switches the machine's mode, and starts the agent's tmux
    session, which forks the conversation under a new session id, so the two never write to the same one."""
    c = cfg()
    if machine_mode(c) != "interactive":
        print(
            "This agent was handed over already: it works in tmux (agent-attach shows it).", file=sys.stderr
        )
        return 1
    words = [a for a in args if not a.startswith("--session=")]
    mode = words.pop(0) if words and words[0] in HOW else "colleague"
    prompt = " ".join(" ".join(words).split()) or "the task, from where the conversation left off."
    sid = this_conversation([a.split("=", 1)[1] for a in args if a.startswith("--session=")])
    if not sid:
        print(
            "Could not tell which conversation to hand over: neither --session=ID nor CLAUDE_CODE_SESSION_ID names "
            "a saved conversation in ~/.claude/projects. Hand over with the /handoff command.",
            file=sys.stderr,
        )
        return 1
    write_rules(c, mode)
    (ROOT / ".claude" / "commands" / "handoff.md").unlink(missing_ok=True)
    new = str(uuid.uuid4())
    write(D / "session_id", new, 0o600)
    save_handoff({"mode": mode, "from": sid, "to": new, "prompt": prompt, "at": int(time.time())})
    drop_inherited()  # (this runs under the Claude Code in VS Code: its variables must not reach the agent)
    spawn_watchdog()
    ensure_agent_session(c)
    # (at most 90 s: Claude Code cuts a Bash command off after 2 minutes by default)
    how = "now continues" if agent_started(c, 90) else "is starting (it can take a minute) to continue"
    print(
        "Handed over: a %s agent %s this conversation in tmux on this machine. Stop here: end your turn without "
        "further work, and tell the user they can close VS Code." % (mode, how)
    )
    return 0


def this_conversation(given: list):
    """The conversation agent-handoff runs in: the id /handoff passed, else the one Claude Code gives its Bash
    commands (when Claude runs agent-handoff itself); None when neither names a saved conversation."""
    for sid in [*given, os.environ.get("CLAUDE_CODE_SESSION_ID", "")]:
        if sid and transcript(sid):
            return sid
    return None


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
                    "Refused: this machine may push only to %s, not %s." % (c["branch"], what),
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
                        "Refused: %s contains the value of secret %s. Use the environment variable instead."
                        % (current, secret),
                        file=sys.stderr,
                    )
                    return 1
    for f in [x for x in git("diff", "--cached", "--name-only", "--diff-filter=AM", "-z").split("\0") if x]:
        path = Path(c["repo_dir"]) / f
        if path.is_file() and path.stat().st_size > 50_000_000:
            print(
                "Refused: %s is %.1f GB: keep big files out of git (add it to .gitignore; put outputs in "
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
        "send": lambda: cmd_send(sys.stdin.read()),
        "save": lambda: cmd_save(args),
        "declare": lambda: cmd_declare(args),
        "handoff": lambda: cmd_handoff(args),
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
