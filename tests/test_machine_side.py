"""The machine side (remote/cs.py, remote/fake_claude.py) against a temporary root: CS_ROOT, CS_BIN and
CS_PROFILE_D point into tmp_path, "GitHub" is a local bare repo, and the agent's git hooks run cs.py the way
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
from types import SimpleNamespace

import pytest
from fake_gh import git

REMOTE = Path(__file__).resolve().parents[1] / "src" / "crab_student" / "remote"
CS_PY, FAKE_CLAUDE = REMOTE / "cs.py", REMOTE / "fake_claude.py"
T0 = 1_700_000_000.0
HF = "hf_" + "Zq8x" * 8  # a task secret's value
TOKEN = "sk-ant-oat01-" + "Tk7w" * 20  # the Claude token
KEY_LINE = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW"  # (deploy key, line 1)
NOTE = "agent-runs/job1/stopped.json"  # the stop note, in the repo
MACHINE_FILES = ("rules.md", "rules-autonomous.md", "rules-colleague.md", "handoff.md")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX (flock, sh scripts, symlinks)")


# ---- fixtures and helpers ------------------------------------------------------------------------


@pytest.fixture
def cs(tmp_path, monkeypatch):
    """cs.py imported afresh with its paths under tmp_path/root, and an agent's config.json."""
    root = tmp_path / "root"
    monkeypatch.setenv("CS_ROOT", str(root))
    monkeypatch.setenv("CS_BIN", str(tmp_path / "usr-local-bin"))
    monkeypatch.setenv("CS_PROFILE_D", str(tmp_path / "profile.d"))
    monkeypatch.setenv("CS_TICK_SECONDS", "60")
    monkeypatch.setenv("CS_VENV", str(tmp_path / "venv-main"))  # (the image's environment: none until made)
    for var in ("CLAUDE_CODE_SESSION_ID", "TERM_PROGRAM"):  # (this test may itself run inside Claude Code)
        monkeypatch.delenv(var, raising=False)
    spec = importlib.util.spec_from_file_location("cs", CS_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.D.mkdir(parents=True)
    config = dict(name="job1", branch="agent/job1", repo_url=str(tmp_path / "origin.git"),
                  repo_dir=str(root / "work" / "proj"), mode="colleague", claude_settings={}, fake_claude=False,
                  session_id=str(uuid.uuid4()), idle_minutes=60, notify_url="", secret_names=["HF_TOKEN"])  # fmt: skip
    (module.D / "config.json").write_text(json.dumps(config))
    return module


def set_config(cs, **changes) -> dict:
    c = dict(cs.cfg(), **changes)
    (cs.D / "config.json").write_text(json.dumps(c))
    return c


def machine_files(cs) -> None:
    """The rules, the /handoff command and the brief, as the upload leaves them in D."""
    for name in MACHINE_FILES:
        shutil.copy(REMOTE / name, cs.D / name)
    (cs.D / "BRIEF.md").write_text("# Brief\n\nTrain the model.\n")


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
    """The agent's clone as setup leaves it, with hooks that run `cs.py hook` like the machine's."""
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


def set_agent(cs, state: str, message: str = "") -> None:
    (cs.D / "agent.json").write_text(json.dumps({"state": state, "message": message, "at": 0}))


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


def make_transcript(cs, sid: str, folder: str = "-root-work-proj") -> Path:
    path = cs.ROOT / ".claude" / "projects" / folder / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type": "user"}\n')
    return path


def handed_off(cs, **fields) -> dict:
    """handoff.json as agent-handoff leaves it (from: a new VS Code conversation, unless given), with changes."""
    h = dict({"mode": "colleague", "prompt": "finish the tests", "at": 0}, **fields)
    if "from" not in h:
        h["from"] = make_transcript(cs, str(uuid.uuid4())).stem
    (cs.D / "handoff.json").write_text(json.dumps(h))
    return h


def wait_for(condition, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline and not condition():
        time.sleep(0.1)
    return condition()


# ---- the watchdog's rules (tick) -----------------------------------------------------------------


class FakeProbe:
    def __init__(self) -> None:
        self.bytes, self.cpu, self.gpu, self.copy = 0, 0.0, False, False

    def transcript_bytes(self) -> int:
        return self.bytes

    def cpu_seconds(self) -> float:
        return self.cpu

    def gpu_busy(self) -> bool:
        return self.gpu

    def copying(self) -> bool:
        return self.copy

    def work(self, minute: int) -> None:
        self.bytes += 100  # the conversation grows


def watchdog(cs, **config):
    """The state right after a container start at T0, a quiet fake probe, and the config with changes."""
    st = cs.load_state()
    st.update(boot=T0, last_activity=T0, last_tick=T0, last_talk=T0, cpu_seconds=0.0)
    return st, FakeProbe(), set_config(cs, **config)


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


def first_stop(seen: dict):
    return next(((a, m) for m, actions in seen.items() for a in actions if a.startswith("stop:")), None)


@pytest.mark.parametrize(
    "activity, stop", [(None, 60), ("transcript", 110), ("cpu", 110), ("gpu", 110), ("copy", 110)]
)
def test_the_machine_stops_after_idle_minutes_of_quiet_ticks(cs, activity, stop):
    """Quiet: an unchanged transcript, no GPU, no copy of the outputs under way, and idle Claude Code, tmux and
    sshd under a quarter core."""
    st, probe, c = watchdog(cs, idle_minutes=60)

    def minute(m):  # quiet, but for one kind of activity at minute 50
        probe.cpu += 15.0 if activity == "cpu" and m == 50 else 14.0
        probe.gpu = activity == "gpu" and m == 50
        probe.copy = activity == "copy" and m == 50
        if activity == "transcript" and m == 50:
            probe.work(m)

    seen = run_minutes(cs, st, probe, c, range(1, 111), minute)
    assert minutes_with(seen, "stop:idle")[0] == stop
    assert not any(seen[m] for m in range(1, stop))


@pytest.mark.parametrize("state, stop", [("done", "stop:done"), ("working", "stop:idle"),
                                         ("needs-you", "stop:idle"), (None, "stop:idle")])  # fmt: skip
def test_every_agent_stops_after_idle_minutes_done_or_not_and_the_reason_says_which(cs, state, stop):
    st, probe, c = watchdog(cs, idle_minutes=45)
    if state:
        set_agent(cs, state)
    seen = run_minutes(cs, st, probe, c, range(1, 61))
    assert first_stop(seen) == (stop, 45)


def test_a_running_job_keeps_a_finished_agents_machine_up(cs):
    st, probe, c = watchdog(cs, idle_minutes=60)
    set_agent(cs, "done")

    def job_until_100(minute):  # (a job the agent left running uses the GPU until minute 100)
        probe.gpu = minute <= 100

    seen = run_minutes(cs, st, probe, c, range(1, 200), job_until_100)
    assert first_stop(seen) == ("stop:done", 160)


@pytest.mark.parametrize("mode, state, nudges", [("colleague", "needs-you", [30, 70]), ("colleague", "working", []),
                                                 ("colleague", "done", []), ("autonomous", "needs-you", []),
                                                 ("interactive", "needs-you", [])])  # fmt: skip
def test_only_a_colleague_that_asked_is_nudged_once_per_quiet_stretch(cs, mode, state, nudges):
    st, probe, c = watchdog(cs, idle_minutes=60, mode=mode)
    set_agent(cs, state)

    def answered_at_40(minute):
        if minute == 40:
            probe.work(minute)

    seen = run_minutes(cs, st, probe, c, range(1, 100), answered_at_40)
    assert minutes_with(seen, "nudge") == nudges


@pytest.mark.parametrize("mode, state, check_ins", [("colleague", "working", [20, 60]), ("autonomous", "working", [20, 60]),
                                                    ("interactive", "working", []), ("colleague", "needs-you", []),
                                                    ("colleague", "done", [])])  # fmt: skip
def test_a_check_in_comes_once_per_silent_stretch_to_an_agent_in_tmux(cs, mode, state, check_ins):
    st, probe, c = watchdog(cs, idle_minutes=600, mode=mode)
    set_agent(cs, state)

    def busy_and_a_word_at_40(minute):  # (a job keeps the CPU and the GPU busy; the agent says nothing)
        probe.cpu, probe.gpu = probe.cpu + 60.0, True
        if minute == 40:
            probe.work(minute)

    seen = run_minutes(cs, st, probe, c, range(1, 80), busy_and_a_word_at_40)
    assert minutes_with(seen, "check-in") == check_ins


def test_after_a_hand_off_the_watchdog_treats_the_agent_by_its_new_mode(cs):
    st, probe, c = watchdog(cs, idle_minutes=600, mode="interactive")
    set_agent(cs, "working")
    assert "check-in" not in [
        a for actions in run_minutes(cs, st, probe, c, range(1, 30)).values() for a in actions
    ]
    handed_off(cs, mode="autonomous")
    assert (cs.machine_mode(c), cs.frontend(c)) == ("autonomous", "tmux")
    seen = run_minutes(cs, st, probe, c, range(30, 60))
    assert minutes_with(seen, "check-in") == [30]


def test_a_long_gap_counts_at_most_two_ticks_and_machine_time_survives_a_watchdog_restart(cs):
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
            set_agent(cs, state)
        notified += [a for a in cs.tick(st, c, T0 + 60 * m, probe) if a.startswith("notify:")]
    assert notified == ["notify:needs-you", "notify:needs-you", "notify:done", "notify:failed"]


def test_conversations_count_in_tmux_and_in_vs_code_subagents_too(cs):
    probe = cs.Probe()
    before = probe.transcript_bytes()
    make_transcript(cs, "vscode-session", folder="-root-work-proj")
    grown = probe.transcript_bytes()
    make_transcript(cs, "vscode-session/subagents/agent-1", folder="-root-work-proj")
    assert before < grown < probe.transcript_bytes()


def test_a_copy_of_the_outputs_counts_as_activity_only_while_it_is_under_way(cs):
    probe = cs.Probe()
    assert not probe.copying()
    cs.COPYING.parent.mkdir(parents=True, exist_ok=True)
    cs.COPYING.write_text("")  # (the copy touches it every half minute while bytes flow)
    assert probe.copying()
    for when in (time.time() - 2 * cs.TICK - 1, time.time() + 86400):  # (stalled; a time yet to come)
        os.utime(cs.COPYING, (when, when))
        assert not probe.copying()


# ---- save, check, status, send -------------------------------------------------------------------


def test_save_commits_and_pushes_what_is_safe_and_skips_big_untracked_files(cs, repo, bare, sized):
    (repo / "README.md").write_text("changed\n")
    (repo / "notes.txt").write_text("notes\n")
    sized(repo / "data" / "big.bin", 11_000_000)
    res = cs.save("idle")
    assert res["error"] is None and res["pushed"] and res["committed"] == 2
    assert res["skipped"] == ["data/big.bin"] and res["dirty"] == 1 and res["unpushed"] == 0
    assert git(bare, "log", "-1", "--format=%s", "agent/job1").strip() == "Autosave (idle)"
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
    res = cs.save("idle")
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
    left = note["outputs"]
    assert (left["files"], left["bytes"], left["sample"]) == (1, 2000, ["run1/model.pt"])
    assert left["digest"] == cs.fingerprint(cs.OUTPUTS, ["run1/model.pt"])  # (for a delete after a fetch)
    assert note["large_files"] == [{"path": "ckpt/last.pt", "bytes": 100_000_000}]
    assert (note["not_committed"], note["not_committed_count"]) == (["data/big.bin"], 1)
    assert git(bare, "log", "-1", "--format=%s", "agent/job1").strip() == "Autosave (idle)"
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
    assert {**note["outputs"], "digest": ""} == {"files": 0, "bytes": 0, "sample": [], "digest": ""}
    assert note["large_files"] == note["not_committed"] == [] and note["not_committed_count"] == 0


@pytest.mark.parametrize("stopped_before", [False, True])
def test_a_refused_final_save_commits_no_note_and_restores_the_note_file(cs, repo, bare, stopped_before):
    earlier = cs.save("idle", final=True)["note"] if stopped_before else None
    secret_files(cs, {"HF_TOKEN": HF})
    (repo / "leak.py").write_text(f"token = '{HF}'\n")
    res = cs.save("done", final=True)
    assert "HF_TOKEN" in res["error"] and res["note"]["reason"] == "done" and not res["note_committed"]
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
    set_agent(cs, "working", "epoch 3/10")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "a")
    (repo / "README.md").write_text("changed\n")
    assert cs.cmd_status() == 0
    st = last_json(capsys)
    assert set(st) == {"name", "mode", "frontend", "agent", "idle_seconds", "used_hours", "stopping",
                       "stop_reason", "stop_sent_for", "git"}  # fmt: skip
    assert st["git"] == {"branch": "agent/job1", "head": head(repo)[:7], "dirty": 1, "unpushed": 1}
    assert st["agent"]["message"] == "epoch 3/10" and st["stop_sent_for"] is None
    assert (st["mode"], st["frontend"]) == ("colleague", "tmux")
    assert st["used_hours"] == 1.5 and 120 <= st["idle_seconds"] < 180


def test_status_shows_an_interactive_agent_in_vs_code_until_it_is_handed_off(cs, repo, capsys):
    def where() -> tuple:
        assert cs.cmd_status() == 0
        st = last_json(capsys)
        return st["mode"], st["frontend"]

    set_config(cs, mode="interactive")
    assert where() == ("interactive", "vscode")
    handed_off(cs, mode="autonomous")
    assert where() == ("autonomous", "tmux")


def test_send_to_an_agent_in_vs_code_says_where_to_type(cs, capsys, monkeypatch):
    set_config(cs, mode="interactive")
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n"))
    assert cs.main(["send"]) == 1
    assert "--vscode" in last_json(capsys)["error"]


def test_a_failing_command_prints_one_json_error(cs, capsys):
    assert cs.main(["no-such-command"]) == 2
    assert cs.main(["set-limit", "3"]) == 2  # (there is no time limit)
    (cs.D / "config.json").unlink()
    assert cs.main(["status"]) == 1 and "error" in last_json(capsys)


# ---- agent-status and the git hooks --------------------------------------------------------------


def test_declare_done_needs_the_report_committed_and_everything_pushed(cs, repo, capsys):
    assert cs.cmd_declare(["working", "training"]) == 0
    before = cs.read_agent()

    def refused() -> bool:
        return cs.cmd_declare(["done", "all good"]) == 1 and cs.read_agent() == before

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
    assert cs.cmd_declare(["done", "all good"]) == 0 and cs.read_agent()["state"] == "done"


def test_declare_checks_the_state_and_keeps_the_message_to_one_short_line(cs, capsys):
    assert cs.cmd_declare(["finished", "x"]) == 2 and not (cs.D / "agent.json").exists()
    assert cs.cmd_declare(["needs-you", "Which\ndataset", "  should I use?", "x" * 200]) == 0
    message = cs.read_agent()["message"]
    assert message.startswith("Which dataset should I use? xxx") and len(message) == 120


def test_an_autonomous_agent_cannot_ask_and_is_told_to_decide_and_record_it(cs, capsys):
    set_config(cs, mode="autonomous")
    assert cs.cmd_declare(["working", "training"]) == 0
    assert cs.cmd_declare(["needs-you", "Which dataset?"]) == 1
    assert cs.read_agent()["state"] == "working" and "Decisions" in capsys.readouterr().out


def test_declare_alone_shows_the_status_and_the_machine_time(cs, capsys):
    set_agent(cs, "working", "epoch 3/10")
    cs.save_state(dict(cs.load_state(), used_seconds=1800))
    assert cs.cmd_declare([]) == 0
    shown = capsys.readouterr().out
    assert "working" in shown and "epoch 3/10" in shown
    assert "0h30m" in shown and "60 min" in shown  # the machine time, and when it stops by itself


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
def test_pre_push_allows_only_the_agents_own_branch(cs, monkeypatch, capsys, line, allowed):
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


def test_nothing_the_agent_reads_names_the_tool(cs):
    """Rules, prompts, watchdog and hook messages describe the situation plainly: the agent has no context
    for the name crab-student."""
    texts = [(REMOTE / name).read_text() for name in MACHINE_FILES]
    texts += [cs.FIRST_PROMPT, cs.LOST, cs.RESTART_NOTE, cs.NUDGE, cs.CHECK_IN, cs.HANDOFF_PROMPT]
    assert not any("crab" in text.lower() or "worker" in text.lower() for text in texts)
    for message in (cs.RESTART_NOTE, cs.NUDGE, cs.CHECK_IN, cs.HANDOFF_PROMPT):
        assert message.startswith("Automated message from the machine:")


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
    set_agent(cs, "needs-you", "Which dataset?")
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
    assert (payload["agent"], payload["event"]) == ("job1", "needs-you") and "job1" in payload["text"]
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
    assert git(bare, "log", "-1", "--format=%s", "agent/job1").strip() == "Autosave (idle)"
    assert (stop["method"], stop["url"]) == ("PUT", "http://vast.invalid/api/v0/instances/42/")
    assert json.loads(stop["body"]) == {"state": "stopped"}
    assert stop["headers"]["authorization"] == "Bearer key-42"


def test_a_done_stop_says_so_and_records_its_stop_call_only_once_saved(cs, repo, bare, sink, monkeypatch,
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
    assert payload["event"] == "stopped" and "done" in payload["message"]
    assert seen_while_saving == [None] and head(bare, "agent/job1") == head(repo) and stop["method"] == "PUT"
    assert cs.cmd_status() == 0
    st = last_json(capsys)
    assert st["stop_reason"] == "done" and 0 <= st["stop_sent_for"] < 60


@pytest.mark.parametrize("reason", ["idle", "done"])
def test_a_watchdog_stop_pushes_the_stop_note_with_its_reason(cs, repo, bare, sink, reason):
    (repo / "README.md").write_text("changed\n")
    vast_env = {"CS_VAST_URL": "http://vast.invalid", "CONTAINER_ID": "42", "CONTAINER_API_KEY": "key-42"}
    cs.perform("stop:" + reason, cs.load_state(), cs.cfg(), vast_env)
    assert pushed_note(bare)["reason"] == reason and last_commit_files(bare) == ["README.md", NOTE]
    assert head(bare, "agent/job1") == head(repo) and git(repo, "status", "--porcelain") == ""
    assert [r["method"] for r in sink] == ["PUT"]  # (then Vast is asked to stop the machine)


# ---- Claude Code's environment and command line --------------------------------------------------


def test_agent_env_drops_a_parent_claudes_and_vasts_variables_and_adds_the_secrets(cs, monkeypatch):
    dropped = {
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",
        "CONTAINER_API_KEY",
        "CONTAINER_ID",
    }
    for var in dropped:
        monkeypatch.setenv(var, "1")
    monkeypatch.delenv("TERM", raising=False)
    odd = "'single' and \"double\" quotes, spaces, $HOME and `x`"
    secret_files(cs, {"HF_TOKEN": HF, "ODD": odd})
    env = cs.agent_env(cs.cfg())
    assert not dropped & set(env)
    assert env["IS_SANDBOX"] == "1" and env["DISABLE_AUTOUPDATER"] == "1" and env["TERM"]
    assert (env["AGENT_NAME"], env["AGENT_BRANCH"]) == ("job1", "agent/job1")
    assert env["PATH"].startswith(str(cs.ROOT)) and env["PATH"].endswith(os.environ["PATH"])
    assert (env["CLAUDE_CODE_OAUTH_TOKEN"], env["HF_TOKEN"], env["ODD"]) == (TOKEN, HF, odd)


def test_claude_argv_for_a_new_a_resumed_and_a_restarted_conversation(cs):
    c = cs.cfg()
    sid = c["session_id"]
    new = cs.claude_argv(c, sid, first=True, lost=False)
    assert new[0] == str(cs.CLAUDE) and "--dangerously-skip-permissions" in new and "--model" not in new
    assert new[-3:] == ["--session-id", sid, cs.FIRST_PROMPT]
    assert cs.claude_argv(c, sid, first=True, lost=True)[-1] == cs.FIRST_PROMPT + cs.LOST
    make_transcript(cs, sid)
    c.update(fake_claude=True, claude_settings={"model": "opus"})
    restarted = cs.claude_argv(c, sid, first=True, lost=False)
    assert restarted[:2] == [cs.PY, str(cs.D / "fake_claude.py")] and "--model" not in restarted
    assert restarted[-3:] == ["--resume", sid, cs.RESTART_NOTE]
    assert cs.claude_argv(c, sid, first=False, lost=False)[-2:] == ["--resume", sid]


def test_claude_argv_after_a_hand_off_forks_the_vs_code_conversation(cs):
    c = set_config(cs, mode="interactive")
    sid, h = c["session_id"], handed_off(cs, mode="autonomous", prompt="finish the tests")
    fork = cs.claude_argv(c, sid, first=True, lost=False)
    assert fork[-6:-1] == ["--resume", h["from"], "--fork-session", "--session-id", sid]
    assert fork[-1].startswith("Automated message from the machine:") and fork[-1].endswith(
        "finish the tests"
    )
    assert "without asking" in fork[-1]  # (autonomous)


class EndOfTest(Exception):
    pass


def run_claude(cs, monkeypatch, claude, presses: int) -> list:
    """cmd_claude with `claude(argv)` as Claude Code (its exit code); the user presses Enter after it exits
    `presses` times, then the test ends. Returns the command lines."""
    calls, entered = [], []

    def call(argv, cwd, env):
        calls.append(argv)
        return claude(argv)

    def enter(*args):
        if len(entered) == presses:
            raise EndOfTest
        entered.append(1)
        return ""

    monkeypatch.setattr(cs.subprocess, "call", call)
    monkeypatch.setattr("builtins.input", enter)
    with pytest.raises(EndOfTest):
        cs.cmd_claude()
    return calls


def test_claude_starts_a_new_conversation_when_the_saved_one_cannot_be_resumed(cs, monkeypatch):
    old = cs.cfg()["session_id"]
    make_transcript(cs, old)
    results = iter([1, 0, 0])  # the first resume fails at once

    def claude(argv):
        if "--session-id" in argv:  # a new conversation is saved as soon as it starts
            make_transcript(cs, argv[argv.index("--session-id") + 1])
        return next(results)

    calls = run_claude(cs, monkeypatch, claude, presses=1)
    new = (cs.D / "session_id").read_text().strip()
    assert new != old
    assert calls[0][-3:] == ["--resume", old, cs.RESTART_NOTE]
    assert calls[1][-3:] == ["--session-id", new, cs.FIRST_PROMPT + cs.LOST]
    assert calls[2][-2:] == ["--resume", new]  # (the restart note only on the first run)


def test_a_forked_conversation_is_resumed_under_its_own_id_after_it_exits(cs, monkeypatch):
    c = set_config(cs, mode="interactive")
    h = handed_off(cs)
    (cs.D / "session_id").write_text("f0f0f0f0-0000-4000-8000-000000000001")  # (what agent-handoff chose)

    def claude(argv):
        if "--fork-session" in argv:
            make_transcript(cs, argv[argv.index("--session-id") + 1])
        return 0

    calls = run_claude(cs, monkeypatch, claude, presses=1)
    new = cs.session_id(c)
    assert new == "f0f0f0f0-0000-4000-8000-000000000001" and cs.transcript(new)
    assert calls[0][-6:-2] == ["--resume", h["from"], "--fork-session", "--session-id"]
    assert calls[1][-2:] == ["--resume", new]


def test_a_conversation_that_cannot_be_forked_starts_anew_with_what_the_user_asked(cs, monkeypatch):
    c = set_config(cs, mode="interactive")
    (cs.D / "session_id").write_text("f0f0f0f0-0000-4000-8000-000000000002")  # (what agent-handoff chose)
    handed_off(cs, prompt="finish the tests")

    def claude(argv):
        if "--fork-session" in argv:
            return 1  # (refused at once)
        if "--session-id" in argv:
            make_transcript(cs, argv[argv.index("--session-id") + 1])
        return 0

    calls = run_claude(cs, monkeypatch, claude, presses=1)
    assert [("--fork-session" in argv) for argv in calls] == [True, False, False]
    new = cs.session_id(c)
    assert new not in ("f0f0f0f0-0000-4000-8000-000000000002", cs.read_handoff()["from"])
    assert calls[1][-3:-1] == ["--session-id", new] and calls[1][-1].startswith(cs.FIRST_PROMPT + cs.LOST)
    assert calls[1][-1].endswith("finish the tests") and cs.read_handoff()["fork_failed"]
    assert calls[2][-2:] == ["--resume", new]  # (the new conversation from then on, never a fork again)


# ---- the hand-off --------------------------------------------------------------------------------


@pytest.fixture
def interactive(cs, monkeypatch):
    """An interactive agent after setup: its rules files in D, the /handoff command installed, a VS Code
    conversation (`sid`), and Claude in tmux as good as started. `started` gets, for each tmux session
    agent-handoff starts, the parent Claude Code's variables it would carry. (The environment is a copy:
    agent-handoff edits it.)"""
    machine_files(cs)
    c = set_config(cs, mode="interactive")
    cs.setup_settings(c, {})
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setattr(cs, "spawn_watchdog", lambda: None)
    monkeypatch.setattr(cs, "agent_started", lambda c, seconds: True)
    started = []
    monkeypatch.setattr(
        cs, "ensure_agent_session", lambda c: started.append(sorted(k for k in os.environ if cs.inherited(k)))
    )
    return SimpleNamespace(sid=make_transcript(cs, str(uuid.uuid4())).stem, started=started)


def test_handoff_switches_the_mode_writes_the_rules_and_starts_the_agent_in_tmux(
    cs, interactive, monkeypatch, capsys
):
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):  # (it runs under the Claude Code in VS Code)
        monkeypatch.setenv(var, "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", interactive.sid)
    assert not (cs.ROOT / ".claude" / "CLAUDE.md").exists()
    assert cs.cmd_handoff(["autonomous", "finish", "the", "tests"]) == 0
    assert "close VS Code" in capsys.readouterr().out
    h = cs.read_handoff()
    assert (h["mode"], h["from"], h["prompt"]) == ("autonomous", interactive.sid, "finish the tests")
    c = cs.cfg()
    assert (cs.machine_mode(c), cs.frontend(c)) == ("autonomous", "tmux") and c["mode"] == "interactive"
    assert "## Work on your own" in (cs.ROOT / ".claude" / "CLAUDE.md").read_text()
    assert not (cs.ROOT / ".claude" / "commands" / "handoff.md").exists()
    assert cs.session_id(c) not in (c["session_id"], interactive.sid)
    assert interactive.started == [[]]  # (once, with none of the VS Code Claude's variables)
    assert cs.cmd_handoff(["colleague"]) == 1 and cs.read_handoff() == h  # (once only)


@pytest.mark.parametrize("given", ["argument", "environment", "neither"])
def test_handoff_hands_over_the_conversation_it_runs_in_and_never_guesses(
    cs, interactive, monkeypatch, given
):
    """The id /handoff passes, else the one Claude Code gives its Bash commands (when Claude runs agent-handoff
    itself); not the newest conversation."""
    newest = make_transcript(cs, str(uuid.uuid4())).stem
    session = {"argument": interactive.sid, "environment": "", "neither": str(uuid.uuid4())}[given]
    env = {"argument": newest, "environment": interactive.sid, "neither": ""}[given]  # (the argument wins)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", env)
    code = cs.cmd_handoff(
        [f"--session={session}", "colleague", "go on"]
    )  # ("": ${CLAUDE_SESSION_ID} not filled)
    if given == "neither":
        assert code == 1 and cs.read_handoff() == {} and interactive.started == []
        assert cs.frontend(cs.cfg()) == "vscode"
    else:
        assert code == 0 and interactive.started == [[]]
        assert (cs.read_handoff()["from"], cs.read_handoff()["prompt"]) == (interactive.sid, "go on")


def test_handoff_defaults_to_a_colleague_continuing_the_task(cs, interactive, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", interactive.sid)
    assert cs.cmd_handoff([]) == 0
    h = cs.read_handoff()
    assert h["mode"] == "colleague" and h["prompt"]
    assert "## Work like a capable colleague" in (cs.ROOT / ".claude" / "CLAUDE.md").read_text()


# ---- setup ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode, section", [("colleague", "## Work like a capable colleague"),
                                           ("autonomous", "## Work on your own")])  # fmt: skip
def test_setup_settings_fills_the_rules_and_merges_claude_json(cs, mode, section):
    machine_files(cs)
    (cs.ROOT / ".claude.json").write_text(json.dumps({"theme": "dark", "projects": {"/other": {"x": 1}}}))
    c = set_config(cs, idle_minutes=47, mode=mode)
    for _ in range(2):  # (setup runs again at every start)
        cs.setup_settings(c, {})
    rules = (cs.ROOT / ".claude" / "CLAUDE.md").read_text()
    assert "{{" not in rules and "}}" not in rules and section in rules
    for value in (
        "job1",
        "agent/job1",
        c["repo_dir"],
        "agent-runs/job1/REPORT.md",
        "HF_TOKEN",
        "47",
        str(cs.BRIEF),
    ):
        assert value in rules
    assert "Train the model." in cs.BRIEF.read_text() and not (cs.D / "BRIEF.md").exists()  # (one brief)
    assert (
        not (cs.ROOT / ".claude" / "commands" / "handoff.md").exists() and not (cs.D / "handoff.md").exists()
    )
    state = json.loads((cs.ROOT / ".claude.json").read_text())
    assert state["theme"] == "dark" and state["projects"]["/other"] == {"x": 1}
    assert state["hasCompletedOnboarding"]
    settings = json.loads((cs.ROOT / ".claude" / "settings.json").read_text())
    assert settings[
        "skipDangerousModePermissionPrompt"
    ]  # (the bypass warning would block an unattended start)
    trusted = {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}
    assert state["projects"][c["repo_dir"]] == trusted


def test_an_interactive_agent_gets_no_rules_but_the_handoff_command(cs):
    machine_files(cs)
    cs.setup_settings(set_config(cs, mode="interactive"), {})
    assert not (cs.ROOT / ".claude" / "CLAUDE.md").exists()
    command = (cs.ROOT / ".claude" / "commands" / "handoff.md").read_text()
    assert "agent-handoff --session=${CLAUDE_SESSION_ID}" in command and "$ARGUMENTS" in command


def test_claude_settings_carry_the_launch_choices_the_token_and_the_secrets(cs):
    machine_files(cs)
    secret_files(cs, {"HF_TOKEN": HF})
    per_model = {"claude-opus-5-5": {"effortLevel": "xhigh"}}
    chosen = {"model": "opus", "effortLevel": "high", "fastMode": True, "modelSettings": per_model,
              "permissions": {"allow": ["*"]}}  # fmt: skip
    cs.setup_settings(set_config(cs, claude_settings=chosen), {})
    path = cs.ROOT / ".claude" / "settings.json"
    settings = json.loads(path.read_text())
    assert (settings["model"], settings["effortLevel"], settings["fastMode"]) == ("opus", "high", True)
    assert settings["modelSettings"] == per_model
    assert settings["permissions"]["defaultMode"] == "bypassPermissions" and settings["permissions"]["deny"]
    assert "allow" not in settings["permissions"]  # (only the model, effort and fast mode are taken)
    env = settings["env"]
    assert (env["CLAUDE_CODE_OAUTH_TOKEN"], env["HF_TOKEN"], env["IS_SANDBOX"]) == (TOKEN, HF, "1")
    assert env["CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK"] == "1"  # (else the token's fast mode stays off)
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    vscode = json.loads((cs.ROOT / ".vscode-server" / "data" / "Machine" / "settings.json").read_text())
    assert vscode["claudeCode.initialPermissionMode"] == "bypassPermissions"
    profile = vscode["terminal.integrated.defaultProfile.linux"]
    assert vscode["terminal.integrated.profiles.linux"][profile]["path"] == str(cs.BIN / "agent-attach")


@pytest.mark.parametrize("mode", ["autonomous", "colleague", "interactive"])
def test_the_machine_is_set_up_from_the_config_the_local_side_writes(cs, mode):
    from crab_student import core, launch

    offer = {
        "id": 7,
        "dph_total": 0.42,
        "cpu_cores_effective": 8,
        "cpu_ram": 16000,
        "geolocation": "Quebec, CA",
    }
    repo = {"slug": "ada/proj-agents", "source": "ada/proj", "branch": "agent/job1",
            "dir": str(cs.ROOT / "work" / "proj")}  # fmt: skip
    a = launch.LaunchArgs("job1", [7], 64, "Train.", None, False, ["HF_TOKEN"], False, False,
                          mode=mode, model="opus", fast=True)  # fmt: skip
    rec = launch.new_record(a, core.load_config(), [offer], repo, None)
    (cs.D / "config.json").write_text(json.dumps(launch.machine_config(rec)))
    machine_files(cs)
    c = cs.cfg()
    cs.setup_settings(c, {})
    assert (cs.machine_mode(c), cs.frontend(c)) == (mode, "vscode" if mode == "interactive" else "tmux")
    settings = json.loads((cs.ROOT / ".claude" / "settings.json").read_text())
    assert (settings["model"], settings["fastMode"]) == ("opus", True)
    rules = cs.ROOT / ".claude" / "CLAUDE.md"
    assert rules.exists() == (mode != "interactive")
    assert (cs.ROOT / ".claude" / "commands" / "handoff.md").exists() == (mode == "interactive")
    assert cs.agent_env(c)["AGENT_BRANCH"] == "agent/job1"
    st, probe, _ = watchdog(cs)
    assert first_stop(run_minutes(cs, st, probe, cs.cfg(), range(1, 62))) == ("stop:idle", 60)


@pytest.mark.parametrize("mode, tools", [("colleague", {"agent-attach", "agent-status"}),
                                         ("interactive", {"agent-attach", "agent-status", "agent-handoff"})])  # fmt: skip
def test_setup_environment_installs_the_tools_the_mode_uses(cs, monkeypatch, mode, tools):
    monkeypatch.setattr(
        cs.shutil, "which", lambda name: "/usr/bin/" + name
    )  # (tmux is there: nothing to install)
    cs.setup_environment(set_config(cs, mode=mode, fake_claude=True), {})
    assert {p.name for p in cs.BIN.iterdir()} == tools
    profile = (cs.PROFILE_D / "agent.sh").read_text()
    assert "claude.env" in profile and "secrets.env" in profile and TOKEN not in profile


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


@posix_only
def test_setup_where_it_was_done_before_only_restarts_the_watchdog(cs, monkeypatch, capsys):
    (cs.D / "setup-done").write_text("")
    ran = []
    for step in ("setup_environment", "setup_repository", "setup_dependencies", "setup_settings", "setup_claude",
                 "setup_start"):  # fmt: skip
        monkeypatch.setattr(cs, step, lambda c, jobs, step=step: ran.append(step))
    monkeypatch.setattr(cs, "spawn_watchdog", lambda: ran.append("watchdog"))
    assert cs.cmd_setup() == 0 and last_json(capsys) == {"ok": True, "resumed": True} and ran == ["watchdog"]


def test_setup_refuses_a_machine_vast_gave_no_key_to_stop_itself(cs):
    (cs.D / "vast.env").write_text("export CONTAINER_ID=42\nexport CONTAINER_API_KEY='unbalanced\n")
    assert cs.read_env_file(cs.D / "vast.env") == {"CONTAINER_ID": "42"}  # (the broken line is skipped)
    with pytest.raises(RuntimeError):
        cs.setup_start(cs.cfg(), {})
    assert not (cs.D / "setup-done").exists()  # (so no watchdog, and no later setup takes it as done)


def test_setup_of_an_interactive_agent_starts_no_claude_in_tmux(cs, monkeypatch):
    (cs.D / "vast.env").write_text("export CONTAINER_ID=42\nexport CONTAINER_API_KEY=key-42\n")
    monkeypatch.setattr(cs, "spawn_watchdog", lambda: None)
    monkeypatch.setattr(cs, "agent_started", lambda c, seconds: pytest.fail("waited for Claude in tmux"))
    cs.setup_start(set_config(cs, mode="interactive"), {})
    assert (cs.D / "setup-done").exists()


# ---- agent-attach --------------------------------------------------------------------------------


class Clock:
    """cs.py's clock in a test: time passes only when it sleeps (a wait for what never comes ends at once)."""

    strftime = staticmethod(time.strftime)

    def __init__(self) -> None:
        self.now = T0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def attach(cs, monkeypatch, session) -> str:
    """agent-attach, with `session(clock)` saying whether the agent's tmux session runs: the program it opens."""
    clock = Clock()
    monkeypatch.setattr(cs, "time", clock)
    monkeypatch.setattr(cs, "spawn_watchdog", lambda: None)
    monkeypatch.setattr(
        cs, "tmux", lambda *a: subprocess.CompletedProcess(a, 0 if session(clock) else 1, "", "")
    )

    def execvp(file, argv):
        raise EndOfTest(file)

    monkeypatch.setattr(cs.os, "execvp", execvp)
    with pytest.raises(EndOfTest) as e:
        cs.cmd_attach()
    return str(e.value)


@pytest.mark.parametrize("mode, session, term, shown, program", [
    ("colleague", True, "", "", "tmux"),
    ("colleague", False, "", "not running", "bash"),
    ("interactive", False, "", "--vscode", "bash"),
    ("interactive", False, "vscode", "", "bash"),  # (a terminal in VS Code: a plain shell)
])  # fmt: skip
def test_attach_opens_the_agents_tmux_or_a_shell(
    cs, monkeypatch, capsys, mode, session, term, shown, program
):
    set_config(cs, mode=mode)
    monkeypatch.setenv("TERM_PROGRAM", term)
    opened = attach(cs, monkeypatch, lambda clock: session)
    out = capsys.readouterr().out
    assert opened == program and (shown in out if shown else out == "")


def test_attach_right_after_a_hand_off_waits_for_the_agents_session(cs, monkeypatch, capsys):
    set_config(cs, mode="interactive")
    handed_off(cs)
    opened = attach(cs, monkeypatch, lambda clock: clock.now >= T0 + 10)  # (up 10 s after the hand-off)
    assert opened == "tmux" and capsys.readouterr().out == ""


# ---- the fake Claude and the watchdog process ----------------------------------------------------


def test_fake_claude_cannot_resume_a_conversation_that_was_never_saved(cs, tmp_path):
    fake = [sys.executable, str(FAKE_CLAUDE), "--resume", str(uuid.uuid4()), "note"]
    assert subprocess.run(fake, cwd=tmp_path, capture_output=True, timeout=30).returncode == 1


@posix_only
def test_fake_claude_works_a_tiny_task_through_to_done_and_forks_like_claude(cs, repo, bare, tmp_path):
    sh_script(tmp_path / "tools" / "agent-status", "declare")
    env = dict(cs.agent_env(cs.cfg()), FAKE_CLAUDE_SLEEP="0")
    env["PATH"] = f"{tmp_path / 'tools'}{os.pathsep}{env['PATH']}"
    sid, log = str(uuid.uuid4()), tmp_path / "fake-claude.log"

    def start(*args: str) -> subprocess.Popen:
        with open(log, "ab") as out:
            return subprocess.Popen([sys.executable, str(FAKE_CLAUDE), "--dangerously-skip-permissions", *args],
                                    cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out)  # fmt: skip

    def run_until(condition, *args: str) -> None:
        fake = start(*args)
        try:
            assert wait_for(condition, 60), log.read_text()
        finally:
            fake.kill()
            fake.wait()

    run_until(lambda: cs.read_agent().get("state") == "done", "--session-id", sid, cs.FIRST_PROMPT)
    pushed = set(git(bare, "ls-tree", "-r", "--name-only", "agent/job1").split())
    assert {"agent-runs/job1/REPORT.md", "agent-runs/job1/result.txt"} <= pushed
    assert (cs.OUTPUTS / "fake-output.bin").exists()
    [transcript] = (cs.ROOT / ".claude" / "projects").glob(f"*/{sid}.jsonl")
    steps = [(s["type"], s["text"]) for s in map(json.loads, transcript.read_text().splitlines())]
    assert ("early-done", "1") in steps  # its "done" before writing the report was refused
    run_until(lambda: '"resumed"' in transcript.read_text(), "--resume", sid, cs.RESTART_NOTE)
    fork = transcript.with_name(str(uuid.uuid4()) + ".jsonl")
    run_until(lambda: fork.exists() and '"forked"' in fork.read_text(),
              "--resume", sid, "--fork-session", "--session-id", fork.stem, "go on")  # fmt: skip
    assert fork.read_text().startswith(transcript.read_text().splitlines()[0])


@posix_only
def test_one_watchdog_per_container_and_machine_time_counts_from_the_latest_start(cs, tmp_path):
    tmux = tmp_path / "tools" / "tmux"
    tmux.parent.mkdir()
    tmux.write_text("#!/bin/sh\nexit 0\n")  # the agent's session "exists"
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
        assert 0 <= st["used_seconds"] < 20 and st["stopping"] is None and (cs.D / "heartbeat").exists()
    finally:
        first.kill()
        first.wait()


# ---- the dependencies, the Python environment, VS Code's server -----------------------------------


def make_python(venv: Path) -> Path:
    """A Python environment's interpreter (an empty file: only its place matters)."""
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "bin" / "python").write_text("")
    return venv / "bin" / "python"


@pytest.fixture
def spawned(cs, monkeypatch) -> list:
    """The background processes cs.py would start (`cs.py watch`), never started."""
    jobs = []
    monkeypatch.setattr(cs, "spawn", lambda command, logname: jobs.append(command))
    return jobs


@pytest.mark.parametrize("files, setup, installs", [
    ({"uv.lock": "", "pyproject.toml": "[project]\nname = 'p'\n"}, None, ["uv sync"]),
    ({"pyproject.toml": "[project]\nname = 'p'\n\n[tool.uv]\ndev-dependencies = []\n"}, None, ["uv sync"]),
    ({"pyproject.toml": "[project]\nname = 'p'\n", "requirements.txt": "numpy\n"}, None, ["pip install -e ."]),
    ({"requirements.txt": "numpy\n", "requirements-dev.txt": "pytest\n", "setup.py": ""}, None,
     ["-r requirements-dev.txt", "-r requirements.txt"]),
    ({"setup.py": ""}, None, ["pip install -e ."]),
    ({"README.md": "# proj\n"}, None, []),
    ({"requirements.txt": "numpy\n"}, "make deps", ["make deps"]),
    ({"requirements.txt": "numpy\n"}, "none", []),
])  # fmt: skip
def test_the_dependency_install_is_the_first_the_repo_fits_or_what_the_launch_said(
    cs, files, setup, installs
):
    c = set_config(cs, setup=setup)
    repo = Path(c["repo_dir"])
    repo.mkdir(parents=True)
    for name, text in files.items():
        (repo / name).write_text(text)
    command = cs.deps_command(c)
    assert all(part in command for part in installs) and bool(command) == bool(installs)
    assert ("pip install" in command) <= (cs.python() in command)  # (pip runs as the environment's Python)


def test_pip_installs_with_the_images_python_when_it_has_one(cs, tmp_path):
    c = cs.cfg()
    Path(c["repo_dir"]).mkdir(parents=True)
    (Path(c["repo_dir"]) / "setup.py").write_text("")
    assert cs.python() == "python3" and cs.deps_command(c).startswith("python3 ")
    make_python(cs.VENV)
    assert shlex.split(cs.deps_command(c))[0] == str(cs.VENV / "bin" / "python")


def setup_installing_only(cs, monkeypatch, capsys, **config) -> tuple:
    """cmd_setup on a repo dir with nothing in it, its steps but the install and the settings skipped: (its
    result, the step lines it printed, the rules)."""
    for step in ("setup_environment", "setup_repository", "setup_claude", "setup_start"):
        monkeypatch.setattr(cs, step, lambda c, jobs: None)
    machine_files(cs)
    Path(set_config(cs, **config)["repo_dir"]).mkdir(parents=True)
    (cs.D / "logs").mkdir()
    assert cs.cmd_setup() == 0
    lines = capsys.readouterr().out.splitlines()
    steps = [line.split()[1] for line in lines if line.startswith("step ")]
    return json.loads(lines[-1]), steps, (cs.ROOT / ".claude" / "CLAUDE.md").read_text()


@posix_only
@pytest.mark.parametrize("command, code", [
    ("mkdir -p .venv/bin && touch .venv/bin/python && echo Installed", 0),
    ("echo Resolving; echo 'error: no matching distribution for torch' >&2; exit 3", 3),
])  # fmt: skip
def test_setup_installs_the_dependencies_before_the_agent_starts_and_a_failure_is_only_a_warning(
    cs, monkeypatch, capsys, command, code
):
    result, steps, rules = setup_installing_only(cs, monkeypatch, capsys, setup=command)
    assert result["ok"] and steps == [f"{i}/6" for i in range(1, 7)]
    assert (cs.D / "heartbeat").exists()  # (setup keeps the dead-man switch from stopping a long install)
    assert cs.read_deps() == {"command": command, "exit_code": code}
    vscode = json.loads((cs.VSCODE_HOME / "data" / "Machine" / "settings.json").read_text())
    if code == 0:
        assert result["warnings"] == [] and command in rules
        venv = (
            Path(cs.cfg()["repo_dir"]) / ".venv" / "bin" / "python"
        )  # (the settings come after the install)
        assert vscode["python.defaultInterpreterPath"] == str(venv)
    else:
        [warning] = result["warnings"]
        assert "no matching distribution for torch" in warning and str(code) in warning
        assert command in rules and "Resolving" in rules and "no matching distribution for torch" in rules
        assert str(cs.D) not in rules  # (the agent gets the log's end, never the tool's folder)


@posix_only
def test_an_install_that_does_not_end_is_stopped_with_all_it_started(cs, monkeypatch):
    monkeypatch.setattr(cs, "DEPS_TIMEOUT", 2)
    c = set_config(cs, setup="sleep 300 & echo $! > child.pid; wait")
    repo = Path(c["repo_dir"])
    repo.mkdir(parents=True)
    (cs.D / "logs").mkdir()
    started = time.time()
    cs.setup_dependencies(c, {})
    assert time.time() - started < 60 and cs.read_deps()["exit_code"] != 0 and cs.deps_warnings()
    child = int((repo / "child.pid").read_text())

    def running() -> bool:  # (a killed process is gone, or a zombie until it is reaped)
        try:
            return Path(f"/proc/{child}/stat").read_text().split(") ")[-1][0] != "Z"
        except OSError:
            return False

    assert wait_for(lambda: not running(), 10)


@pytest.mark.parametrize("files, setup", [({"README.md": "# proj\n"}, None),
                                          ({"requirements.txt": "numpy\n"}, "none")])  # fmt: skip
def test_with_nothing_to_install_setup_installs_nothing_and_the_rules_say_so(cs, monkeypatch, files, setup):
    machine_files(cs)
    c = set_config(cs, setup=setup)
    repo = Path(c["repo_dir"])
    repo.mkdir(parents=True)
    for name, text in files.items():
        (repo / name).write_text(text)
    monkeypatch.setattr(cs.subprocess, "Popen", lambda *a, **kw: pytest.fail("an install was started"))
    cs.setup_dependencies(c, {})
    cs.setup_settings(c, {})
    assert "command" not in cs.read_deps() and cs.deps_warnings() == []
    rules = (cs.ROOT / ".claude" / "CLAUDE.md").read_text()
    assert "requirements.txt" in rules and "{{" not in rules  # (how to set things up the repo's way)


def test_the_rules_give_the_end_of_a_failed_installs_output_but_not_where_its_log_is(cs):
    machine_files(cs)
    (cs.D / "logs").mkdir()
    lines = [f"Collecting package-{i}" for i in range(30)] + ["error: no torch build for CUDA 11.2"]
    (cs.D / "logs" / "deps.log").write_text("\n".join(lines) + "\n")
    (cs.D / "deps.json").write_text(json.dumps({"command": "uv sync", "exit_code": 2}))
    cs.setup_settings(cs.cfg(), {})
    rules = (cs.ROOT / ".claude" / "CLAUDE.md").read_text()
    assert "uv sync" in rules and "error: no torch build for CUDA 11.2" in rules and "package-29" in rules
    assert "package-0\n" not in rules and str(cs.D) not in rules  # (the end only; never the tool's folder)
    [warning] = cs.deps_warnings()
    assert "uv sync" in warning and "error: no torch build for CUDA 11.2" in warning


def test_an_interactive_agent_has_agent_status_to_look_but_no_status_to_set(cs, capsys):
    set_config(cs, mode="interactive")
    assert cs.cmd_declare([]) == 0 and "60 min" in capsys.readouterr().out  # (when the machine stops)
    assert cs.cmd_declare(["working", "on it"]) == 1 and not (cs.D / "agent.json").exists()


def test_shells_claude_and_vs_code_use_the_repos_venv_once_there_is_one_else_the_images(cs, tmp_path):
    machine_files(cs)
    c = cs.cfg()
    vscode = cs.VSCODE_HOME / "data" / "Machine" / "settings.json"
    venv = Path(c["repo_dir"]) / ".venv"
    env = cs.agent_env(c)
    assert env["PATH"].startswith(c["repo_dir"] + "/.venv/bin:") and "VIRTUAL_ENV" not in env  # (none yet)
    make_python(cs.VENV)
    cs.setup_settings(c, {})
    assert cs.agent_env(c)["VIRTUAL_ENV"] == str(cs.VENV)
    assert json.loads(vscode.read_text())["python.defaultInterpreterPath"] == str(cs.VENV / "bin" / "python")
    make_python(venv)
    cs.write_vscode_settings(c)
    assert cs.agent_env(c)["VIRTUAL_ENV"] == str(venv)
    settings = json.loads(vscode.read_text())
    assert settings["python.defaultInterpreterPath"] == str(venv / "bin" / "python")
    assert settings["claudeCode.initialPermissionMode"] == "bypassPermissions"  # (the rest stays)


def test_the_login_profile_activates_the_environment_and_starts_in_the_repo_and_bashrc_runs_it_once(
    cs, monkeypatch, spawned
):
    monkeypatch.setattr(cs.shutil, "which", lambda name: "/usr/bin/" + name)
    (cs.ROOT / ".bashrc").parent.mkdir(parents=True, exist_ok=True)
    (cs.ROOT / ".bashrc").write_text("# the image's own\n. /venv/main/bin/activate")
    (cs.ROOT / ".config" / "git").mkdir(parents=True)
    (cs.ROOT / ".config" / "git" / "ignore").write_text("*.swp\n")
    c = set_config(cs, fake_claude=True)
    for _ in range(2):
        cs.setup_environment(c, {})
    profile = (cs.PROFILE_D / "agent.sh").read_text()
    for part in (f"{c['repo_dir']}/.venv/bin", str(cs.VENV), "VIRTUAL_ENV", "cd ", "claude.env"):
        assert part in profile, part
    bashrc = (cs.ROOT / ".bashrc").read_text()
    assert (
        bashrc.startswith("# the image's own") and bashrc.count(str(cs.PROFILE_D / "agent.sh")) == 2
    )  # (one line)
    assert bashrc.splitlines()[-1].endswith(str(cs.PROFILE_D / "agent.sh"))  # (after the image's activation)
    assert spawned == []  # (no VS Code here: nothing to prepare for it)
    ignored = (
        (cs.ROOT / ".config" / "git" / "ignore").read_text().splitlines()
    )  # (what installs leave behind)
    assert ignored[0] == "*.swp" and ignored.count(".venv/") == ignored.count("*.egg-info/") == 1


COMMIT = "0123456789abcdef0123456789abcdef01234567"


def test_an_interactive_agents_setup_puts_vs_codes_server_where_remote_ssh_looks(cs, monkeypatch):
    monkeypatch.setattr(cs.shutil, "which", lambda name: "/usr/bin/" + name)
    started = []
    real = subprocess.Popen  # (git still runs; the install jobs, started through bash, are only recorded)
    monkeypatch.setattr(
        cs.subprocess,
        "Popen",
        lambda argv, **kw: started.append(argv[-1]) if argv[0] == "bash" else real(argv, **kw),
    )
    jobs = {}
    cs.setup_environment(set_config(cs, fake_claude=True, vscode_commit=COMMIT), jobs)
    [command] = started
    assert list(jobs) == ["VS Code"]
    assert f"commit:{COMMIT}/server-linux-" in command
    assert str(cs.VSCODE_HOME / "cli" / "servers" / f"Stable-{COMMIT}" / "server") in command
    assert "--install-extension anthropic.claude-code" in command
    started.clear()
    cs.setup_environment(set_config(cs, vscode_commit=None), {})
    assert started == []


@pytest.mark.parametrize("code, warned", [(0, False), (1, True)])
def test_setup_waits_for_vs_codes_server_and_only_warns_when_it_failed(cs, code, warned):
    (cs.D / "logs").mkdir()
    (cs.D / "logs" / "vscode.log").write_text("curl: (6) Could not resolve host\n")
    job = subprocess.Popen([sys.executable, "-c", f"raise SystemExit({code})"])
    warnings = cs.vscode_server_ready({"VS Code": job})
    assert bool(warnings) == warned and all("Could not resolve host" in w for w in warnings)
    assert cs.vscode_server_ready({}) == []
