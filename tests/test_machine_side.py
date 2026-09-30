"""The machine side (remote/cs.py, remote/fake_claude.py) against a temporary root: CS_ROOT, CS_BIN and
CS_PROFILE_D point into tmp_path, "GitHub" is a local bare repo, and the worker's git hooks run cs.py the way
the machine's do. What needs a POSIX system (flock, sh scripts on PATH, symlinks) is skipped on Windows."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fake_gh import git

REMOTE = Path(__file__).resolve().parents[1] / "src" / "crab_student" / "remote"
CS_PY, FAKE_CLAUDE = REMOTE / "cs.py", REMOTE / "fake_claude.py"
T0 = 1_700_000_000.0
HF = "hf_" + "Zq8x" * 8  # a task secret's value
TOKEN = "sk-ant-oat01-" + "Tk7w" * 20  # the Claude token
KEY_LINE = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW"  # (deploy key, line 1)
NOTE = "agent-runs/job1/stopped.json"  # the stop note, in the repo
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX (flock, sh scripts, symlinks)")


# ---- fixtures and helpers ------------------------------------------------------------------------


@pytest.fixture
def cs(tmp_path, monkeypatch):
    """cs.py imported afresh with its paths under tmp_path/root, and a worker's config.json."""
    root = tmp_path / "root"
    monkeypatch.setenv("CS_ROOT", str(root))
    monkeypatch.setenv("CS_BIN", str(tmp_path / "usr-local-bin"))
    monkeypatch.setenv("CS_PROFILE_D", str(tmp_path / "profile.d"))
    monkeypatch.setenv("CS_TICK_SECONDS", "60")
    spec = importlib.util.spec_from_file_location("cs", CS_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.D.mkdir(parents=True)
    config = dict(name="job1", branch="agent/job1", repo_url=str(tmp_path / "origin.git"),
                  repo_dir=str(root / "work" / "proj"), model="", fake_claude=False, session_id=str(uuid.uuid4()),
                  idle_minutes=60, limit_hours=2.0, notify_url="", secret_names=["HF_TOKEN"])  # fmt: skip
    (module.D / "config.json").write_text(json.dumps(config))
    return module


@pytest.fixture
def bare(tmp_path) -> Path:
    """'GitHub': a bare repo whose main and agent/job1 hold one commit."""
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "README.md").write_text("# proj\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-q", "-m", "Initial commit")
    git(tmp_path, "init", "-q", "--bare", "-b", "main", "origin.git")
    git(seed, "push", "-q", str(tmp_path / "origin.git"), "main", "main:agent/job1")
    return tmp_path / "origin.git"


@pytest.fixture
def repo(cs, bare) -> Path:
    """The worker's clone as setup leaves it, with hooks that run `cs.py hook` like the machine's."""
    for hook in ("pre-commit", "pre-push"):
        sh_script(cs.D / "hooks" / hook, "hook " + hook)
    cs.setup_repository(cs.cfg(), {})
    return Path(cs.cfg()["repo_dir"])


def sh_script(path: Path, command: str) -> None:
    """`#!/bin/sh` + exec cs.py <command> (git runs hooks through its own sh on Windows too)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    python, script = Path(sys.executable).as_posix(), CS_PY.as_posix()
    path.write_text(f'#!/bin/sh\nexec "{python}" "{script}" {command} "$@"\n', newline="\n")
    path.chmod(0o755)


def head(repo: Path, ref: str = "HEAD") -> str:
    return git(repo, "rev-parse", ref).strip()


def pushed_note(bare: Path) -> dict:
    return json.loads(git(bare, "show", f"agent/job1:{NOTE}"))


def last_commit_files(bare: Path) -> list:
    return sorted(git(bare, "show", "--name-only", "--format=", "agent/job1").split())


def last_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.splitlines()[-1])


def set_worker(cs, state: str, message: str = "") -> None:
    (cs.D / "worker.json").write_text(json.dumps({"state": state, "message": message, "at": 0}))


def secret_files(cs, secrets: dict, token: str = TOKEN) -> None:
    """claude.env and secrets.env as the local side writes them: export lines with shell-quoted values."""
    (cs.D / "claude.env").write_text(f"export CLAUDE_CODE_OAUTH_TOKEN={shlex.quote(token)}\n")
    (cs.D / "secrets.env").write_text("".join(f"export {k}={shlex.quote(v)}\n" for k, v in secrets.items()))


@pytest.fixture
def sized():
    """sized(path, size) makes a file of `size` bytes without writing them; it is removed afterwards."""
    made = []

    def make(path: Path, size: int) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.truncate(size)
        made.append(path)
        return path

    yield make
    for path in made:
        path.unlink(missing_ok=True)


def make_transcript(cs, sid: str) -> None:
    path = cs.ROOT / ".claude" / "projects" / "-root-work-proj" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type": "user"}\n')


def wait_for(condition, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline and not condition():
        time.sleep(0.1)
    return condition()


# ---- the watchdog's rules (tick) -----------------------------------------------------------------


class FakeProbe:
    def __init__(self) -> None:
        self.bytes, self.cpu, self.gpu = 0, 0.0, False

    def transcript_bytes(self) -> int:
        return self.bytes

    def cpu_seconds(self) -> float:
        return self.cpu

    def gpu_busy(self) -> bool:
        return self.gpu

    def work(self, minute: int) -> None:
        self.bytes += 100  # the conversation grows


def watchdog(cs, **config):
    """The state right after a container start at T0, a quiet fake probe, and the config with changes."""
    st = cs.load_state()
    st.update(boot=T0, last_activity=T0, last_tick=T0, last_talk=T0, cpu_seconds=0.0)
    return st, FakeProbe(), dict(cs.cfg(), **config)


def run_minutes(cs, st, probe, c, minutes, before=None) -> dict:
    """A tick at T0 + 60 s * m for each minute m (`before(m)` changes the probe first): {m: actions}."""
    seen = {}
    for m in minutes:
        if before:
            before(m)
        seen[m] = cs.tick(st, c, T0 + 60 * m, probe)
    return seen


def minutes_with(seen: dict, action: str) -> list:
    return [m for m, actions in seen.items() if action in actions]


@pytest.mark.parametrize("activity, stop", [(None, 60), ("transcript", 110), ("cpu", 110), ("gpu", 110)])
def test_the_machine_stops_after_idle_minutes_of_quiet_ticks(cs, activity, stop):
    """Quiet: an unchanged transcript, no GPU, and idle Claude Code, tmux and sshd under a quarter core."""
    st, probe, c = watchdog(cs, idle_minutes=60, limit_hours=10)

    def minute(m):  # quiet, but for one kind of activity at minute 50
        probe.cpu += 15.0 if activity == "cpu" and m == 50 else 14.0
        probe.gpu = activity == "gpu" and m == 50
        if activity == "transcript" and m == 50:
            probe.work(m)

    seen = run_minutes(cs, st, probe, c, range(1, 111), minute)
    assert minutes_with(seen, "stop:idle")[0] == stop
    assert not any(seen[m] for m in range(1, stop))


@pytest.mark.parametrize("state, nudges", [("needs-you", [30, 70]), ("working", []), ("done", [])])
def test_the_nudge_comes_once_per_quiet_stretch_and_only_for_needs_you(cs, state, nudges):
    st, probe, c = watchdog(cs, idle_minutes=60, limit_hours=10)
    set_worker(cs, state)

    def answered_at_40(minute):
        if minute == 40:
            probe.work(minute)

    seen = run_minutes(cs, st, probe, c, range(1, 100), answered_at_40)
    assert minutes_with(seen, "nudge") == nudges


@pytest.mark.parametrize("state, check_ins", [("working", [20, 60]), ("needs-you", []), ("done", [])])
def test_a_check_in_comes_once_per_silent_stretch_however_busy_the_machine(cs, state, check_ins):
    st, probe, c = watchdog(cs, idle_minutes=600, limit_hours=20)
    set_worker(cs, state)

    def busy_and_a_word_at_40(minute):  # (a job keeps the CPU and the GPU busy; the worker says nothing)
        probe.cpu, probe.gpu = probe.cpu + 60.0, True
        if minute == 40:
            probe.work(minute)

    seen = run_minutes(cs, st, probe, c, range(1, 80), busy_and_a_word_at_40)
    assert minutes_with(seen, "check-in") == check_ins


@pytest.mark.parametrize("state, stop", [("done", ("stop:done", 15)), ("working", ("stop:idle", 60)),
                                         ("needs-you", ("stop:idle", 60))])  # fmt: skip
def test_a_finished_worker_stops_after_15_quiet_minutes_the_others_after_idle_minutes(cs, state, stop):
    st, probe, c = watchdog(cs, idle_minutes=60, limit_hours=10)
    set_worker(cs, state)
    seen = run_minutes(cs, st, probe, c, range(1, 61))
    assert next((a, m) for m, actions in seen.items() for a in actions if a.startswith("stop:")) == stop


def test_the_warning_comes_once_before_the_limit_and_again_after_an_extend(cs):
    st, probe, c = watchdog(cs, limit_hours=1)
    seen = run_minutes(cs, st, probe, c, range(1, 60), probe.work)
    assert minutes_with(seen, "warn") == [50]  # 10 minutes before the limit
    c["limit_hours"] = 2  # crab-student extend (set-limit)
    seen = run_minutes(cs, st, probe, c, range(60, 121), probe.work)
    assert minutes_with(seen, "warn") == [110]
    assert minutes_with(seen, "stop:time-limit") == [120]


def test_the_time_limit_stop_waits_for_the_boot_grace(cs):
    st, probe, c = watchdog(cs, limit_hours=1)
    st["used_seconds"] = 3600.0  # started again with no time left
    seen = run_minutes(cs, st, probe, c, range(1, 7), probe.work)
    assert minutes_with(seen, "stop:time-limit") == [5, 6]  # from 5 minutes after the container start


def test_a_long_gap_counts_at_most_two_ticks_and_used_time_survives_a_restart(cs):
    st, probe, c = watchdog(cs)
    cs.tick(st, c, T0 + 60, probe)
    cs.tick(st, c, T0 + 60 + 3600, probe)  # the watchdog was not running for an hour
    cs.tick(st, c, T0, probe)  # the clock went back
    assert st["used_seconds"] == 60 + 2 * 60
    cs.save_state(st)
    assert cs.load_state()["used_seconds"] == 180


def test_notifications_come_once_per_transition_to_needs_you_done_or_failed(cs):
    st, probe, c = watchdog(cs)
    states = [None, "working", "needs-you", "needs-you", "working", "needs-you", "done", "done", "failed"]
    notified = []
    for m, state in enumerate(states, 1):
        if state:
            set_worker(cs, state)
        notified += [a for a in cs.tick(st, c, T0 + 60 * m, probe) if a.startswith("notify:")]
    assert notified == ["notify:needs-you", "notify:needs-you", "notify:done", "notify:failed"]


# ---- save, check, status, set-limit --------------------------------------------------------------


def test_save_commits_and_pushes_what_is_safe_and_skips_big_untracked_files(cs, repo, bare, sized):
    (repo / "README.md").write_text("changed\n")
    (repo / "notes.txt").write_text("notes\n")
    sized(repo / "data" / "big.bin", 11_000_000)
    res = cs.save("idle")
    assert res["error"] is None and res["pushed"] and res["committed"] == 2
    assert res["skipped"] == ["data/big.bin"] and res["dirty"] == 1 and res["unpushed"] == 0
    assert git(bare, "log", "-1", "--format=%s", "agent/job1").strip() == "crab-student: autosave (idle)"
    assert git(bare, "ls-tree", "-r", "--name-only", "agent/job1").split() == ["README.md", "notes.txt"]


@pytest.mark.parametrize("count, size, added", [(2060, 2, 2000), (11, 9_500_000, 10)])
def test_save_adds_at_most_2000_untracked_files_or_100_mb(cs, repo, bare, sized, count, size, added):
    """The rest are skipped, and at most 50 of them named. (2,000 paths are too long for one Windows
    command line.)"""
    for i in range(count):
        sized(repo / "results" / f"result-{i:04}.bin", size)
    res = cs.save("idle")
    pushed = set(git(bare, "ls-tree", "-r", "--name-only", "agent/job1").split()) - {"README.md"}
    assert res["error"] is None and res["committed"] == len(pushed) == added
    assert len(res["skipped"]) == min(count - added, 50) and not pushed & set(res["skipped"])


@pytest.mark.parametrize("situation", ["merge", "other branch"])
def test_save_commits_nothing_and_no_note_during_a_merge_or_on_another_branch(cs, repo, bare, situation):
    if situation == "merge":  # (MERGE_HEAD names the commit being merged in)
        theirs = git(repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "theirs")
        (repo / ".git" / "MERGE_HEAD").write_bytes(theirs.encode())  # (LF: git reads it strictly)
    else:
        git(repo, "checkout", "-q", "-b", "experiment")
    before = head(repo)
    (repo / "README.md").write_text("changed\n")
    res = cs.save("user", final=True)
    assert res["committed"] == 0 and res["error"] and res["dirty"] == 1
    assert res["note"] is None and not res["note_committed"] and not (repo / NOTE).exists()
    assert head(repo) == before and head(bare, "agent/job1") == before


def test_save_reports_a_commit_the_hook_refuses_and_leaves_nothing_staged(cs, repo, bare):
    secret_files(cs, {"HF_TOKEN": HF})
    (repo / "README.md").write_text("changed\n")
    (repo / "leak.py").write_text(f"token = '{HF}'\n")
    res = cs.save("time-limit")
    assert res["committed"] == 0 and "HF_TOKEN" in res["error"] and HF not in res["error"]
    assert git(repo, "diff", "--cached", "--name-only") == ""
    assert head(bare, "agent/job1") == head(repo) and (repo / "leak.py").exists()


@posix_only
def test_save_is_not_stopped_by_a_broken_symlink(cs, repo):
    (repo / "latest.ckpt").symlink_to(cs.OUTPUTS / "not-written-yet.ckpt")
    (repo / "README.md").write_text("changed\n")
    res = cs.save("idle")
    assert res["error"] is None and res["committed"] == 2 and res["pushed"]


def test_a_final_save_commits_a_note_of_what_the_machine_leaves_behind(cs, repo, bare, sized):
    """In the autosave's own commit: the outputs, big files git does not track, and what stays uncommitted."""
    (repo / ".gitignore").write_text("ckpt/\n")
    (repo / "README.md").write_text("changed\n")
    sized(cs.OUTPUTS / "run1" / "model.pt", 2000)
    sized(repo / "ckpt" / "last.pt", 100_000_000)  # (ignored)
    sized(repo / "data" / "big.bin", 11_000_000)  # (over the autosave's caps)
    res = cs.save("idle", final=True)
    assert res["error"] is None and res["pushed"] and res["note_committed"]
    assert res["committed"] == 2 and res["dirty"] == 1  # (.gitignore and README.md; data/ is left)
    note = pushed_note(bare)
    assert note == res["note"] and note["reason"] == "idle"
    assert time.time() - 60 < note["stopped_at"] <= time.time()
    assert note["outputs"] == {"files": 1, "bytes": 2000, "sample": ["run1/model.pt"]}
    assert note["large_files"] == [{"path": "ckpt/last.pt", "bytes": 100_000_000}]
    assert (note["not_committed"], note["not_committed_count"]) == (["data/big.bin"], 1)
    assert git(bare, "log", "-1", "--format=%s", "agent/job1").strip() == "crab-student: autosave (idle)"
    assert last_commit_files(bare) == [".gitignore", "README.md", NOTE]


def test_a_plain_save_writes_no_note(cs, repo, bare, capsys):
    (repo / "README.md").write_text("changed\n")
    assert cs.main(["save", "--reason", "manual"]) == 0
    res = last_json(capsys)
    assert res["committed"] == 1 and res["note"] is None and not res["note_committed"]
    assert not (repo / NOTE).exists() and last_commit_files(bare) == ["README.md"]


def test_save_final_commits_the_note_alone_when_nothing_else_changed(cs, repo, bare, capsys):
    assert cs.main(["save", "--reason", "user", "--final"]) == 0
    res = last_json(capsys)
    note = res["note"]
    assert note["reason"] == "user" and res["note_committed"] and res["pushed"] and res["committed"] == 0
    assert last_commit_files(bare) == [NOTE] and pushed_note(bare) == note
    assert note["outputs"] == {"files": 0, "bytes": 0, "sample": []}
    assert note["large_files"] == note["not_committed"] == [] and note["not_committed_count"] == 0


@pytest.mark.parametrize("stopped_before", [False, True])
def test_a_refused_final_save_commits_no_note_and_restores_the_note_file(cs, repo, bare, stopped_before):
    earlier = cs.save("idle", final=True)["note"] if stopped_before else None
    secret_files(cs, {"HF_TOKEN": HF})
    (repo / "leak.py").write_text(f"token = '{HF}'\n")
    res = cs.save("time-limit", final=True)
    assert "HF_TOKEN" in res["error"] and res["note"]["reason"] == "time-limit" and not res["note_committed"]
    assert head(bare, "agent/job1") == head(repo)
    assert (json.loads((repo / NOTE).read_text()) if (repo / NOTE).exists() else None) == earlier
    assert git(repo, "status", "--porcelain").split() == ["??", "leak.py"]  # (no stale note left)


def test_check_reports_outputs_large_untracked_files_and_unpushed_commits(cs, repo, tmp_path, sized, capsys):
    (repo / ".gitignore").write_text("ckpt/\n.venv/\n")
    sized(cs.OUTPUTS / "run1" / "model.pt", 2000)
    (cs.OUTPUTS / "log.txt").write_text("0123456789")
    sized(repo / "ckpt" / "last.pt", 100_000_000)
    sized(repo / ".venv" / "torch.so", 100_000_000)
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "a")
    git(repo, "config", "remote.origin.pushurl", str(tmp_path / "unreachable.git"))  # pushes fail
    assert cs.cmd_check() == 0
    res = last_json(capsys)
    assert res["outputs"]["files"] == 2 and res["outputs"]["bytes"] == 2010
    assert sorted(s.replace("\\", "/") for s in res["outputs"]["sample"]) == ["log.txt", "run1/model.pt"]
    assert res["large_files"] == [{"path": "ckpt/last.pt", "bytes": 100_000_000}]  # (.venv is skipped)
    assert res["unpushed"] == 2 and not res["pushed"]  # a.txt and the autosave of .gitignore


def test_status_reports_the_documented_fields(cs, repo, capsys):
    cs.save_state(dict(cs.load_state(), used_seconds=5400.0, last_activity=time.time() - 120))
    set_worker(cs, "working", "epoch 3/10")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "a")
    (repo / "README.md").write_text("changed\n")
    assert cs.cmd_status() == 0
    st = last_json(capsys)
    assert set(st) == {"name", "worker", "idle_seconds", "used_hours", "limit_hours", "stopping",
                       "stop_reason", "stop_sent_for", "git"}  # fmt: skip
    assert st["git"] == {"branch": "agent/job1", "head": head(repo)[:7], "dirty": 1, "unpushed": 1}
    assert st["worker"]["message"] == "epoch 3/10" and st["stop_sent_for"] is None
    assert (st["used_hours"], st["limit_hours"]) == (1.5, 2.0) and 120 <= st["idle_seconds"] < 180


def test_set_limit_changes_only_the_limit(cs, capsys):
    before = cs.cfg()
    assert cs.cmd_set_limit(8.5) == 0
    assert cs.cfg() == dict(before, limit_hours=8.5) and last_json(capsys)["limit_hours"] == 8.5
    if os.name == "posix":  # (notify_url may hold a webhook secret)
        assert (cs.D / "config.json").stat().st_mode & 0o777 == 0o600


def test_a_failing_command_prints_one_json_error(cs, capsys):
    assert cs.main(["no-such-command"]) == 2
    (cs.D / "config.json").unlink()
    assert cs.main(["set-limit", "3"]) == 1 and "error" in last_json(capsys)


# ---- crab-status and the git hooks ---------------------------------------------------------------


def test_declare_done_needs_the_report_committed_and_everything_pushed(cs, repo, capsys):
    assert cs.cmd_declare(["working", "training"]) == 0
    before = cs.read_worker()

    def refused() -> bool:
        return cs.cmd_declare(["done", "all good"]) == 1 and cs.read_worker() == before

    assert refused()  # no report
    report = repo / "agent-runs" / "job1" / "REPORT.md"
    report.parent.mkdir(parents=True)
    report.write_text("# job1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "report")
    git(repo, "push", "-q")
    (repo / "README.md").write_text("changed\n")
    assert refused()  # a change is not committed
    git(repo, "commit", "-q", "-am", "readme")
    assert refused()  # a commit is not pushed
    git(repo, "push", "-q")
    assert cs.cmd_declare(["done", "all good"]) == 0 and cs.read_worker()["state"] == "done"


def test_declare_checks_the_state_and_keeps_the_message_to_one_short_line(cs, capsys):
    assert cs.cmd_declare(["finished", "x"]) == 2 and not (cs.D / "worker.json").exists()
    assert cs.cmd_declare(["needs-you", "Which\ndataset", "  should I use?", "x" * 200]) == 0
    message = cs.read_worker()["message"]
    assert message.startswith("Which dataset should I use? xxx") and len(message) == 120


def test_declare_alone_shows_the_status_and_the_machine_time(cs, capsys):
    set_worker(cs, "working", "epoch 3/10")
    cs.save_state(dict(cs.load_state(), used_seconds=1800))
    assert cs.cmd_declare([]) == 0
    shown = capsys.readouterr().out
    assert "working" in shown and "epoch 3/10" in shown
    assert "0h30m" in shown and "1h30m" in shown  # used, and left of the 2-hour limit


@pytest.mark.parametrize("secret", ["HF_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "the deploy key"])
def test_pre_commit_refuses_a_secret_value_and_never_prints_it(cs, repo, capsys, secret):
    secret_files(cs, {"HF_TOKEN": HF})
    key = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{KEY_LINE}\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n"
    (cs.D / "deploy_key").write_text(key)
    value = {"HF_TOKEN": HF, "CLAUDE_CODE_OAUTH_TOKEN": TOKEN, "the deploy key": KEY_LINE}[secret]
    (repo / "train.py").write_text(f"print('hi')\nkey = '{value}'\n")
    git(repo, "add", "train.py")
    assert cs.cmd_hook("pre-commit", []) == 1
    err = capsys.readouterr().err
    assert secret in err and "train.py" in err and value not in err


def test_pre_commit_passes_ordinary_changes_and_refuses_a_file_over_50_mb(cs, repo, sized, capsys):
    secret_files(cs, {"SHORT": "abc"})  # (values under 8 characters are not looked for)
    (repo / "train.py").write_text("x = 'abc'\n")
    git(repo, "add", "train.py")
    assert cs.cmd_hook("pre-commit", []) == 0
    sized(repo / "data.bin", 51_000_000)
    git(repo, "add", "data.bin")
    assert cs.cmd_hook("pre-commit", []) == 1
    assert "data.bin" in capsys.readouterr().err


SHA, ZERO = "1" * 40, "0" * 40


@pytest.mark.parametrize("line, allowed", [
    (f"refs/heads/agent/job1 {SHA} refs/heads/agent/job1 {ZERO}", True),
    (f"refs/heads/agent/job1 {SHA} refs/heads/main {SHA}", False),
    (f"refs/tags/v1 {SHA} refs/tags/v1 {ZERO}", False),
    (f"(delete) {ZERO} refs/heads/agent/job1 {SHA}", False),
])  # fmt: skip
def test_pre_push_allows_only_the_workers_own_branch(cs, monkeypatch, capsys, line, allowed):
    monkeypatch.setattr(sys, "stdin", io.StringIO(line + "\n"))
    assert (cs.cmd_hook("pre-push", ["origin", "url"]) == 0) == allowed


def test_the_installed_hooks_stop_pushes_to_other_branches(cs, repo, bare):
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "a")
    main = head(bare, "main")
    for push in (["origin", "HEAD:refs/heads/main"], ["origin", "--delete", "agent/job1"]):
        assert subprocess.run(["git", "-C", str(repo), "push", "-q", *push], capture_output=True).returncode
    assert head(bare, "main") == main
    git(repo, "push", "-q")
    assert head(bare, "agent/job1") == head(repo)


# ---- notifications and the watchdog's stop -------------------------------------------------------


@pytest.fixture
def sink(monkeypatch):
    """A local HTTP server that records requests. It is urllib's proxy too, so every URL (ntfy, Slack,
    Discord, Vast) lands here with its host intact and nothing reaches the network. "missing" paths get 404."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            headers = {k.lower(): v for k, v in self.headers.items()}
            requests.append({"method": self.command, "url": self.path, "headers": headers, "body": body})
            self.send_response(404 if "missing" in self.path else 200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_PUT = do_POST

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for var in ("no_proxy", "NO_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(urllib.request, "_opener", None)  # (urlopen reads the proxy settings when first used)
    yield requests
    server.shutdown()
    server.server_close()


def test_the_watchdog_notifies_each_service_in_its_own_format(cs, sink):
    set_worker(cs, "needs-you", "Which dataset?")
    urls = ["", "http://ntfy.example/crab-topic", "http://hooks.slack.com/services/T0/B0/x",
            "http://discord.com/api/webhooks/1/x", "http://example.invalid/hook"]  # fmt: skip
    for url in urls:  # ("": no notify_url, nothing is sent)
        cs.perform("notify:needs-you", cs.load_state(), dict(cs.cfg(), notify_url=url), {})
    assert [r["url"] for r in sink] == urls[1:]
    ntfy, slack, discord, generic = (r["body"] for r in sink)
    assert b"Which dataset?" in ntfy and "job1" in sink[0]["headers"]["title"]
    assert sink[0]["headers"]["tags"] == "warning"
    assert list(json.loads(slack)) == ["text"] and "Which dataset?" in json.loads(slack)["text"]
    assert list(json.loads(discord)) == ["content"] and "Which dataset?" in json.loads(discord)["content"]
    payload = json.loads(generic)
    assert (payload["worker"], payload["event"]) == ("job1", "needs-you") and "job1" in payload["text"]
    assert payload["message"] == "Which dataset?" and "Which dataset?" in payload["text"]


def test_a_failed_notification_is_logged_without_its_url(cs, sink, capsys):
    url = "http://example.invalid/missing/SECRET-PART"
    cs.notify(dict(cs.cfg(), notify_url=url), "failed", "job1 failed")
    logged = capsys.readouterr().out
    assert len(sink) == 1 and "404" in logged
    assert "SECRET-PART" not in logged and "example.invalid" not in logged


def test_a_watchdog_stop_notifies_saves_and_asks_vast_to_stop(cs, repo, bare, sink):
    (repo / "README.md").write_text("changed\n")
    c = dict(cs.cfg(), notify_url="http://example.invalid/hook")
    vast_env = {"CS_VAST_URL": "http://vast.invalid", "CONTAINER_ID": "42", "CONTAINER_API_KEY": "key-42"}
    cs.perform("stop:idle", cs.load_state(), c, vast_env)
    note, stop = sink
    assert json.loads(note["body"])["event"] == "stopped" and cs.load_state()["stopping"] == "idle"
    assert git(bare, "log", "-1", "--format=%s", "agent/job1").strip() == "crab-student: autosave (idle)"
    assert (stop["method"], stop["url"]) == ("PUT", "http://vast.invalid/api/v0/instances/42/")
    assert json.loads(stop["body"]) == {"state": "stopped"}
    assert stop["headers"]["authorization"] == "Bearer key-42"


def test_a_done_stop_says_finished_and_records_its_stop_call_only_once_saved(cs, repo, bare, sink, monkeypatch,
                                                                            capsys):  # fmt: skip
    real_save, seen_while_saving = cs.save, []

    def save(reason, final=False):
        seen_while_saving.append(cs.load_state()["stop_sent_at"])
        return real_save(reason, final)

    monkeypatch.setattr(cs, "save", save)
    (repo / "README.md").write_text("changed\n")
    c = dict(cs.cfg(), notify_url="http://example.invalid/hook")
    vast_env = {"CS_VAST_URL": "http://vast.invalid", "CONTAINER_ID": "42", "CONTAINER_API_KEY": "key-42"}
    cs.perform("stop:done", cs.load_state(), c, vast_env)
    note, stop = sink
    payload = json.loads(note["body"])
    assert payload["event"] == "stopped" and "finished" in payload["message"]
    assert seen_while_saving == [None] and head(bare, "agent/job1") == head(repo) and stop["method"] == "PUT"
    assert cs.cmd_status() == 0
    st = last_json(capsys)
    assert st["stop_reason"] == "done" and 0 <= st["stop_sent_for"] < 60


@pytest.mark.parametrize("reason", ["idle", "done", "time-limit"])
def test_a_watchdog_stop_pushes_the_stop_note_with_its_reason(cs, repo, bare, sink, reason):
    (repo / "README.md").write_text("changed\n")
    vast_env = {"CS_VAST_URL": "http://vast.invalid", "CONTAINER_ID": "42", "CONTAINER_API_KEY": "key-42"}
    cs.perform("stop:" + reason, cs.load_state(), cs.cfg(), vast_env)
    assert pushed_note(bare)["reason"] == reason and last_commit_files(bare) == ["README.md", NOTE]
    assert head(bare, "agent/job1") == head(repo) and git(repo, "status", "--porcelain") == ""
    assert [r["method"] for r in sink] == ["PUT"]  # (then Vast is asked to stop the machine)


# ---- Claude Code's environment and command line --------------------------------------------------


def test_worker_env_drops_nesting_and_vast_variables_and_adds_the_secrets(cs, monkeypatch):
    dropped = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CONTAINER_API_KEY", "CONTAINER_ID"}
    for var in dropped:
        monkeypatch.setenv(var, "1")
    monkeypatch.delenv("TERM", raising=False)
    odd = "'single' and \"double\" quotes, spaces, $HOME and `x`"
    secret_files(cs, {"HF_TOKEN": HF, "ODD": odd})
    env = cs.worker_env(cs.cfg())
    assert not dropped & set(env)
    assert env["IS_SANDBOX"] == "1" and env["DISABLE_AUTOUPDATER"] == "1" and env["TERM"]
    assert (env["CRAB_STUDENT_NAME"], env["CRAB_STUDENT_BRANCH"]) == ("job1", "agent/job1")
    assert env["PATH"].startswith(str(cs.ROOT)) and env["PATH"].endswith(os.environ["PATH"])
    assert (env["CLAUDE_CODE_OAUTH_TOKEN"], env["HF_TOKEN"], env["ODD"]) == (TOKEN, HF, odd)


def test_claude_argv_for_a_new_a_resumed_and_a_restarted_conversation(cs):
    c = cs.cfg()
    sid = c["session_id"]
    new = cs.claude_argv(c, sid, first=True, lost=False)
    assert new[0] == str(cs.CLAUDE) and "--dangerously-skip-permissions" in new and "--model" not in new
    assert new[-3:] == ["--session-id", sid, cs.FIRST_PROMPT]
    assert cs.claude_argv(c, sid, first=True, lost=True)[-1] == cs.LOST_PROMPT
    make_transcript(cs, sid)
    c.update(model="opus", fake_claude=True)
    restarted = cs.claude_argv(c, sid, first=True, lost=False)
    assert restarted[:2] == [cs.PY, str(cs.D / "fake_claude.py")]
    assert restarted[restarted.index("--model") + 1] == "opus"
    assert restarted[-3:] == ["--resume", sid, cs.RESTART_NOTE]
    assert cs.claude_argv(c, sid, first=False, lost=False)[-2:] == ["--resume", sid]


class EndOfTest(Exception):
    pass


def test_claude_starts_a_new_conversation_when_the_saved_one_cannot_be_resumed(cs, monkeypatch):
    old = cs.cfg()["session_id"]
    make_transcript(cs, old)
    calls = []

    def claude(argv, cwd, env):
        calls.append(argv)
        if "--session-id" in argv:  # a new conversation is saved as soon as it starts
            make_transcript(cs, argv[argv.index("--session-id") + 1])
        return 1 if len(calls) == 1 else 0  # the first resume fails at once

    def enter(*args):  # the user presses Enter after Claude Code exits (once; then the test ends)
        if len(calls) == 3:
            raise EndOfTest
        return ""

    monkeypatch.setattr(cs.subprocess, "call", claude)
    monkeypatch.setattr("builtins.input", enter)
    with pytest.raises(EndOfTest):
        cs.cmd_claude()
    new = (cs.D / "session_id").read_text().strip()
    assert new != old
    assert calls[0][-3:] == ["--resume", old, cs.RESTART_NOTE]
    assert calls[1][-3:] == ["--session-id", new, cs.LOST_PROMPT]
    assert calls[2][-2:] == ["--resume", new]  # (the restart note only on the first run)


# ---- setup ---------------------------------------------------------------------------------------


def test_setup_settings_fills_the_rules_and_merges_claude_json(cs):
    shutil.copy(REMOTE / "worker.md", cs.D / "worker.md")
    (cs.ROOT / ".claude.json").write_text(json.dumps({"theme": "dark", "projects": {"/other": {"x": 1}}}))
    c = dict(cs.cfg(), idle_minutes=47)
    for _ in range(2):  # (setup runs again at every start)
        cs.setup_settings(c, {})
    rules = (cs.ROOT / ".claude" / "CLAUDE.md").read_text()
    assert "{{" not in rules and "}}" not in rules
    for value in ("job1", "agent/job1", c["repo_dir"], "agent-runs/job1/REPORT.md", "HF_TOKEN", "47"):
        assert value in rules
    settings = json.loads((cs.ROOT / ".claude" / "settings.json").read_text())
    assert settings["env"]["IS_SANDBOX"] == "1" and settings["permissions"]["deny"]
    state = json.loads((cs.ROOT / ".claude.json").read_text())
    assert state["theme"] == "dark" and state["projects"]["/other"] == {"x": 1}
    assert state["hasCompletedOnboarding"] and state["bypassPermissionsModeAccepted"]
    trusted = {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}
    assert state["projects"][c["repo_dir"]] == trusted


def test_setup_repository_clones_the_branch_once_and_configures_it(cs, bare):
    c = cs.cfg()
    cs.setup_repository(c, {})
    repo = Path(c["repo_dir"])
    assert git(repo, "symbolic-ref", "--short", "HEAD").strip() == "agent/job1"
    assert git(repo, "config", "core.hooksPath").strip() == str(cs.D / "hooks")
    assert "deploy_key" in git(repo, "config", "core.sshCommand")
    assert git(repo, "config", "remote.origin.push").strip() == "refs/heads/agent/job1:refs/heads/agent/job1"
    (repo / "work.txt").write_text("not lost\n")
    cs.setup_repository(dict(c, repo_url=str(bare.parent / "gone.git")), {})  # a later start: no new clone
    assert (repo / "work.txt").exists()


def test_setup_where_it_was_done_before_only_restarts_the_watchdog(cs, monkeypatch, capsys):
    (cs.D / "setup-done").write_text("")
    ran = []
    for step in ("setup_environment", "setup_repository", "setup_settings", "setup_claude", "setup_start"):
        monkeypatch.setattr(cs, step, lambda c, jobs, step=step: ran.append(step))
    monkeypatch.setattr(cs, "spawn_watchdog", lambda: ran.append("watchdog"))
    assert cs.cmd_setup() == 0 and last_json(capsys) == {"ok": True, "resumed": True} and ran == ["watchdog"]


def test_setup_refuses_a_machine_vast_gave_no_key_to_stop_itself(cs):
    (cs.D / "vast.env").write_text("export CONTAINER_ID=42\nexport CONTAINER_API_KEY='unbalanced\n")
    assert cs.read_env_file(cs.D / "vast.env") == {"CONTAINER_ID": "42"}  # (the broken line is skipped)
    with pytest.raises(RuntimeError):
        cs.setup_start(cs.cfg(), {})
    assert not (cs.D / "setup-done").exists()  # (so no watchdog, and no later setup takes it as done)


# ---- the fake Claude and the watchdog process ----------------------------------------------------


def test_fake_claude_cannot_resume_a_conversation_that_was_never_saved(cs, tmp_path):
    fake = [sys.executable, str(FAKE_CLAUDE), "--resume", str(uuid.uuid4()), "note"]
    assert subprocess.run(fake, cwd=tmp_path, capture_output=True, timeout=30).returncode == 1


@posix_only
def test_fake_claude_works_a_tiny_task_through_to_done(cs, repo, bare, tmp_path):
    sh_script(tmp_path / "tools" / "crab-status", "declare")
    env = dict(cs.worker_env(cs.cfg()), FAKE_CLAUDE_SLEEP="0")
    env["PATH"] = f"{tmp_path / 'tools'}{os.pathsep}{env['PATH']}"
    sid, log = str(uuid.uuid4()), tmp_path / "fake-claude.log"

    def start(*args: str) -> subprocess.Popen:
        with open(log, "ab") as out:
            return subprocess.Popen([sys.executable, str(FAKE_CLAUDE), "--dangerously-skip-permissions", *args],
                                    cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out)  # fmt: skip

    fake = start("--session-id", sid, cs.FIRST_PROMPT)
    try:
        wait_for(lambda: cs.read_worker().get("state") == "done" or fake.poll() is not None, 60)
    finally:
        fake.kill()
        fake.wait()
    assert cs.read_worker().get("state") == "done", log.read_text()
    pushed = set(git(bare, "ls-tree", "-r", "--name-only", "agent/job1").split())
    assert {"agent-runs/job1/REPORT.md", "agent-runs/job1/result.txt"} <= pushed
    assert (cs.OUTPUTS / "fake-output.bin").exists()
    [transcript] = (cs.ROOT / ".claude" / "projects").glob(f"*/{sid}.jsonl")
    steps = [(s["type"], s["text"]) for s in map(json.loads, transcript.read_text().splitlines())]
    assert ("early-done", "1") in steps  # its "done" before writing the report was refused
    resumed = start("--resume", sid, cs.RESTART_NOTE)
    try:
        assert wait_for(lambda: '"resumed"' in transcript.read_text(), 30)
    finally:
        resumed.kill()
        resumed.wait()


@posix_only
def test_one_watchdog_per_container_and_it_keeps_the_used_time_across_starts(cs, tmp_path):
    tmux = tmp_path / "tools" / "tmux"
    tmux.parent.mkdir()
    tmux.write_text("#!/bin/sh\nexit 0\n")  # the worker's session "exists"
    tmux.chmod(0o755)
    (cs.D / "booted").write_text("2000\n")  # a new container start (the previous one was at 1000)
    cs.save_state(dict(cs.load_state(), used_seconds=500.0, boot=1000.0, last_tick=1000.0))
    env = dict(os.environ, PATH=f"{tmux.parent}{os.pathsep}{os.environ['PATH']}", CS_TICK_SECONDS="0.2")
    watch = [sys.executable, str(CS_PY), "watch"]
    first = subprocess.Popen(watch, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_for(lambda: cs.load_state()["boot"] == 2000.0, 30)
        assert subprocess.run(watch, env=env, capture_output=True, timeout=30).returncode == 0  # left at once
        st = cs.load_state()
        assert 500 <= st["used_seconds"] < 520 and st["stopping"] is None and (cs.D / "heartbeat").exists()
    finally:
        first.kill()
        first.wait()
