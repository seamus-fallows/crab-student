"""Agents once launched: display states, views, stop, delete (a stopped one from its stop note), send, report
and open. Records are made by hand; Vast, GitHub and the machines are the fakes."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest
from fake_gh import git

from crab_student import agents, core, github, launch, machine, vast

RUNNING = {"id": 7, "actual_status": "running", "intended_status": "running"}
STOPPED = {"id": 7, "actual_status": "exited", "intended_status": "stopped"}
BOOTING = {"id": 7, "actual_status": "loading", "intended_status": "running", "status_msg": "pulling image"}
CLEAN = {"committed": 0, "pushed": True, "dirty": 0, "unpushed": 0, "error": None,
         "outputs": {"files": 0, "bytes": 0, "sample": []}, "large_files": []}  # fmt: skip
NOTE = "agent-runs/job1/stopped.json"  # where a machine that stopped itself leaves its stop note


def record(name: str = "job1", phase: str = "ready", **fields) -> dict:
    """An agent record with the fields agents read."""
    repo = {"slug": "ada/proj", "branch": f"agent/{name}", "start_sha": None, "deploy_key": None,
            "branch_created": False, "dir": "/root/work/proj"}  # fmt: skip
    return {"name": name, "created": core.now_iso(), "phase": phase, "error": None, "pending": None, "rent": None,
            "rents": [],
            "instance_id": None, "dph": 0.40, "disk_usd_day": None, "mode": "colleague", "claude_settings": {},
            "machine": "1x RTX 4090",
            "ssh": None, "last_seen": None, "last_start": None, "stopped_since": None,
            "repo": repo, "uploaded": phase == "ready", **fields}  # fmt: skip


def agent(fake_vast, name: str = "job1", status: str = "running", **fields) -> int:
    """A ready agent whose machine (rented an hour ago) is running or stopped ("exited") on the fake Vast: its
    instance id."""
    down = {} if status == "running" else {"intended_status": "stopped", "cur_state": "stopped"}
    iid = fake_vast.add_instance(1, f"cs:{name}:0", status=status, **down)
    core.save(record(name, instance_id=iid, **{"last_start": time.time() - 3600, **fields}))
    return iid


def status(state: str | None = None, message: str = "", **fields) -> dict:
    """What ctl.py status answers."""
    return {"agent": {"state": state, "message": message} if state else {}, "mode": "colleague", "frontend": "tmux",
            "idle_seconds": 0, "used_hours": 1.0, "stopping": None, "stop_reason": None,
            "git": {"dirty": 0, "unpushed": 0}, **fields}  # fmt: skip


def note(**fields) -> dict:
    """A stop note as the machine leaves it (on the branch, and in the answer of its final save): written a
    minute ago, nothing left behind."""
    return {"stopped_at": int(time.time()) - 60, "reason": "idle", "outputs": {"files": 0, "bytes": 0, "sample": []},
            "large_files": [], "not_committed": [], "not_committed_count": 0, **fields}  # fmt: skip


def saved(**fields) -> dict:
    """What ctl.py save --final answers: committed and pushed, the stop note in the commit."""
    return {"committed": 1, "pushed": True, "error": None, "dirty": 0, "unpushed": 0, "note": note(),
            "note_committed": True, **fields}  # fmt: skip


def out() -> launch.Progress:
    return launch.Progress(io.StringIO())


def fails(code: int, fn, *a) -> core.CrabError:
    with pytest.raises(core.CrabError) as e:
        fn(*a)
    assert e.value.code == code, e.value.message
    return e.value


@pytest.fixture
def repo(fake_github) -> dict:
    """The record's repo part, with the agent's deploy key and its branch (at the start commit) on GitHub."""
    bare = fake_github.create_repo("ada/proj")
    sha = git(bare, "rev-parse", "main").strip()
    git(bare, "branch", "agent/job1", sha)
    key = {"id": 101, "file": "ada--proj-0123abcd", "created": core.now_iso()}
    fake_github.keys["ada/proj"] = {101: {"title": github.key_title(key)}}
    github.key_file(key).parent.mkdir(parents=True, exist_ok=True)
    github.key_file(key).write_text("private\n")
    Path(f"{github.key_file(key)}.pub").write_text("ssh-ed25519 AAAA shared\n")
    return {"slug": "ada/proj", "branch": "agent/job1", "start_sha": sha, "deploy_key": key,
            "branch_created": True, "dir": "/root/work/proj"}  # fmt: skip


def push_to_branch(fake_github, tmp_path, path: str, text: str) -> None:
    """The agent pushes a file to agent/job1."""
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", "-b", "agent/job1", str(fake_github.bare("ada/proj")), str(work))
    (work / path).parent.mkdir(parents=True, exist_ok=True)
    (work / path).write_text(text)
    git(work, "add", path)
    git(work, "commit", "-q", "-m", "work")
    git(work, "push", "-q", "origin", "agent/job1")


# ---- what list and status show -------------------------------------------------------------------

# fmt: off
DISPLAY = [  # (record fields, Vast's instance, ctl.py status, lock held) -> STATE, and a part of MESSAGE
    ({"phase": "pending", "pending": {"reason": "it would cost $6.00/hr", "max_dph": 6.0}}, None, None, False,
     "approval", "$6.00/hr"),
    ({"phase": "booting"}, None, None, True, "starting", ""),  # (a launch at work)
    ({"phase": "renting"}, None, None, False, "interrupted", "start job1"),
    ({"phase": "setup"}, None, None, False, "interrupted", "start job1"),
    ({"phase": "failed", "error": "every offer failed"}, None, None, False, "failed", "every offer failed"),
    ({}, None, None, False, "failed", "delete job1"),  # (the machine no longer exists on Vast)
    ({}, BOOTING, None, False, "starting", "pulling image"),
    ({"last_seen": {"stop_reason": "idle", "message": "sweep 4/6"}}, STOPPED, None, False, "stopped", "sweep 4/6"),
    ({"last_seen": {"used_hours": 2.0}}, STOPPED, None, False, "stopped", "stopped itself"),  # (idle or done)
    ({"last_seen": {"message": "epoch 3/10"}}, RUNNING, None, False, "unknown", "epoch 3/10"),  # (no answer)
    ({}, RUNNING, status("working", stopping=True, stop_reason="idle"), False, "stopped", "stopping"),
    ({}, RUNNING, status("needs-you", "Drop the old table?"), False, "needs you", "Drop the old table?"),
    ({}, RUNNING, status("done", "see REPORT", git={"dirty": 0, "unpushed": 1}), False, "done", "pushed"),
    ({}, RUNNING, status("done", "see REPORT"), False, "done", "see REPORT"),
    ({}, RUNNING, status("failed", "out of memory"), False, "failed", "out of memory"),
    ({}, RUNNING, status(), False, "starting", ""),  # (the agent has not said anything yet)
    ({}, RUNNING, status(frontend="vscode", mode="interactive"), False, "interactive", "VS Code, active"),
    ({}, RUNNING, status(frontend="vscode", mode="interactive", idle_seconds=720), False, "interactive",
     "VS Code, idle 12m"),
    ({}, RUNNING, status("working", "epoch 3/10", idle_seconds=1380), False, "working", "idle 23m"),
    ({}, RUNNING, status("working", "epoch 3/10", idle_seconds=120), False, "working", "epoch 3/10"),
]
# fmt: on


@pytest.mark.parametrize("fields,inst,st,busy,state,shown", DISPLAY)
def test_display_state(fields, inst, st, busy, state, shown):
    got, message = agents.display_state(record(**fields), inst, st, busy)
    assert got == state and shown in message


def test_display_state_without_vast():
    rec = record(last_seen={"message": "epoch 3/10"})
    assert agents.display_state(rec, None, None, False, vast_down=True)[0] == "unknown"


def test_views_use_live_status_and_remember_what_they_saw(fake_vast, machines):
    agent(fake_vast, "a")
    b = agent(fake_vast, "b", ssh={"host": "old.example", "port": 22})  # (its port moved since)
    last = {"stop_reason": "user", "message": "half done", "used_hours": 1.5}
    agent(fake_vast, "c", status="exited", last_seen=last)
    machines.replies[("a", "status")] = status("working", "epoch 3/10", used_hours=1.25)
    machines.replies[("b", "status")] = machine.Unreachable("could not reach b over ssh")
    views = {v.name: v for v in agents.views()}
    a = views["a"]
    assert (a.state, a.message, a.used_h, a.spent) == ("working", "epoch 3/10", 1.25, 0.5)
    assert views["b"].state == "unknown"
    assert views["c"].state == "stopped" and "half done" in views["c"].message and views["c"].used_h == 1.5
    assert {call[0] for call in machines.calls} == {"a", "b"}  # (no ssh to a stopped machine)
    seen = core.load("a")["last_seen"]
    assert (seen["state"], seen["message"], seen["used_hours"]) == ("working", "epoch 3/10", 1.25)
    host, port = vast.address(fake_vast.instances[b])
    assert core.load("b")["ssh"] == {"host": host, "port": port}
    assert f"Port {port}" in machine.config_file().read_text()


def test_views_say_how_long_a_machine_has_been_stopped_and_what_its_disk_costs(fake_vast, machines):
    iid = agent(fake_vast, status="exited", disk_usd_day=0.17)
    v = agents.views()[0]
    since = core.load("job1")["stopped_since"]
    assert v.state == "stopped" and since and v.stopped_h < 0.1 and v.disk_usd_day == 0.17
    core.save({**core.load("job1"), "stopped_since": since - 3 * 3600})  # (first seen stopped 3 hours ago)
    v = agents.views()[0]
    assert 2.9 < v.stopped_h < 3.1 and core.fmt_hours(3) in v.message and core.usd(0.17) in v.message
    assert core.load("job1")["stopped_since"] == since - 3 * 3600  # (still counted from then)
    fake_vast.instances[iid].update(actual_status="running", intended_status="running", cur_state="running")
    machines.replies[("job1", "status")] = status("working", "epoch 3/10")
    v = agents.views()[0]
    assert v.state == "working" and v.stopped_h is None and core.load("job1")["stopped_since"] is None


@pytest.mark.parametrize("sent_for, stopped", [(None, False), (120, False), (400, True)])
def test_views_stop_a_machine_whose_own_stop_call_vast_did_not_take(sent_for, stopped, fake_vast, machines):
    iid = agent(fake_vast)
    machines.replies[("job1", "status")] = status("done", stopping="done", stop_sent_for=sent_for)
    agents.views()
    assert (fake_vast.instances[iid]["intended_status"] == "stopped") is stopped  # (the fake checks the key)
    assert len(fake_vast.calls("PUT", "/")) == stopped


def test_views_carry_mode_frontend_and_settings(fake_vast, machines):
    settings = {"model": "opus", "effortLevel": "high"}
    iid = agent(fake_vast, claude_settings=settings, mode="interactive")
    machines.replies[("job1", "status")] = status(frontend="vscode", mode="interactive")
    v = agents.views()[0]
    assert (v.mode, v.frontend, v.settings) == ("interactive", "vscode", settings)
    assert v.state == "interactive"
    fake_vast.instances[iid].update(actual_status="exited", intended_status="stopped", cur_state="stopped")
    assert agents.views()[0].frontend == "vscode"  # (remembered while stopped)


def test_a_hand_off_changes_the_records_mode(fake_vast, machines):
    agent(fake_vast, mode="interactive")
    machines.replies[("job1", "status")] = status("working", "epoch 1/10", mode="colleague", frontend="tmux")
    v = agents.views()[0]
    assert (v.mode, v.frontend, v.state) == ("colleague", "tmux", "working")
    assert core.load("job1")["mode"] == "colleague"


# ---- stop ----------------------------------------------------------------------------------------


def test_stop_saves_while_the_machine_still_runs_then_stops_it(fake_vast, monkeypatch):
    iid = agent(fake_vast)
    saves = []  # (the command, and the machine's state while it ran)

    def remote(name, args, **kw):
        saves.append((args[0], fake_vast.instances[iid]["actual_status"]))
        return {"committed": 2, "pushed": True, "error": None}

    monkeypatch.setattr(machine, "remote", remote)
    agents.stop("job1", out())
    assert saves == [("save", "running")] and fake_vast.instances[iid]["intended_status"] == "stopped"
    rec = core.load("job1")
    assert rec["last_seen"]["stop_reason"] == "user" and rec["stopped_since"] == pytest.approx(
        time.time(), abs=60
    )


def test_stop_saves_with_the_stop_note_and_keeps_none_of_it_in_the_record(fake_vast, machines):
    agent(fake_vast)
    machines.replies[("job1", "save")] = saved()
    agents.stop("job1", out())
    assert "--final" in machines.calls[0][1]  # (the save that pushes the note to the branch)
    assert not core.load("job1").get("stop_note")  # (the branch is its one source)


@pytest.mark.parametrize(
    "error", [machine.Unreachable("could not reach job1"), core.CrabError("job1: git broke")]
)
def test_stop_still_stops_when_the_save_fails(error, fake_vast, machines):
    iid = agent(fake_vast)
    machines.replies[("job1", "save")] = error
    agents.stop("job1", out())
    assert fake_vast.instances[iid]["intended_status"] == "stopped"


def test_stopping_a_stopped_agent_changes_nothing(fake_vast, machines):
    agent(fake_vast, status="exited")
    agents.stop("job1", out())
    assert not fake_vast.calls("PUT", "/") and not machines.calls


@pytest.mark.parametrize("state", ["running", "exited"])
def test_a_launch_cut_off_after_its_upload_may_hold_work(state, fake_vast, fake_github, machines):
    """Delete checks it (a stopped one that left no stop note cannot be checked: --force), and stop stops it."""
    iid = agent(fake_vast, status=state, phase="setup", uploaded=True)
    machines.replies[("job1", "check")] = {**CLEAN, "unpushed": 1}
    machines.replies[("job1", "save")] = {"committed": 1, "pushed": True, "error": None}
    fails(3, agents.delete, "job1", False, out())
    assert iid in fake_vast.instances and core.record_path("job1").exists()
    agents.stop("job1", out())
    assert not vast.wants_running(fake_vast.instances[iid])


# ---- delete --------------------------------------------------------------------------------------


def test_deleting_a_pending_agent_only_forgets_it(fake_vast):
    core.save(record(phase="pending", pending={"reason": "it would cost $6.00/hr", "max_dph": 6.0}))
    agents.delete("job1", False, out())
    assert not core.record_path("job1").exists() and not fake_vast.requests


@pytest.mark.parametrize(
    "problem,named",
    [
        ({"unpushed": 2}, ""),
        ({"dirty": 1}, ""),
        ({"outputs": {"files": 3, "bytes": 5.1e9, "sample": ["best.pt"]}}, "best.pt"),
        ({"large_files": [{"path": "runs/ckpt.pt", "bytes": 2.1e9}]}, "runs/ckpt.pt"),
    ],
)
def test_delete_refuses_when_work_would_be_lost(problem, named, fake_vast, machines):
    iid = agent(fake_vast)
    machines.replies[("job1", "check")] = {**CLEAN, **problem}
    assert named in fails(3, agents.delete, "job1", False, out()).message
    assert iid in fake_vast.instances and core.record_path("job1").exists()


@pytest.mark.parametrize("worked", [False, True])
def test_a_clean_check_destroys_the_machine_and_keeps_only_a_branch_with_work(
    worked, fake_vast, fake_github, machines, repo, tmp_path
):
    agent(fake_vast, repo=repo)
    if worked:
        push_to_branch(fake_github, tmp_path, "result.txt", "42\n")
    machines.replies[("job1", "check")] = CLEAN
    agents.delete("job1", False, out())
    assert [call[1] for call in machines.calls] == [["check"]]
    assert not fake_vast.instances and not core.record_path("job1").exists()
    assert fake_github.keys["ada/proj"] == {}  # (the deploy key is gone)
    assert not github.key_file(repo["deploy_key"]).exists()
    assert (fake_github.branch_sha("ada/proj", "agent/job1") is not None) is worked


def test_a_shared_deploy_key_goes_with_the_last_agent_holding_it(fake_vast, fake_github, machines, repo):
    git(fake_github.bare("ada/proj"), "branch", "agent/job2", repo["start_sha"])
    agent(fake_vast, repo=repo)
    agent(fake_vast, "job2", repo={**repo, "branch": "agent/job2"})
    machines.replies[("job1", "check")] = machines.replies[("job2", "check")] = CLEAN
    agents.delete("job1", False, out())
    assert list(fake_github.keys["ada/proj"]) == [101] and github.key_file(repo["deploy_key"]).exists()
    agents.delete("job2", False, out())
    assert fake_github.keys["ada/proj"] == {} and not github.key_file(repo["deploy_key"]).exists()


def rent(label: str, instance_id: int | None = None) -> dict:
    """A rent of the launch's race, as the record keeps it."""
    return {"label": label, "offer_id": 1, "at": 0, "instance_id": instance_id, "running_at": None}


def test_delete_destroys_the_machines_of_a_race_cut_off_even_those_vast_did_not_confirm(fake_vast):
    racing = fake_vast.add_instance(1, "cs:job1:aaaa1111", status="loading")
    unconfirmed = fake_vast.add_instance(1, "cs:job1:abcd1234", status="loading")
    other = fake_vast.add_instance(1, "cs:job1:00000000", status="loading")  # (not this launch's label)
    rents = [rent("cs:job1:aaaa1111", racing), rent("cs:job1:abcd1234"), rent("cs:job1:ffff0000")]
    core.save(record(phase="renting", uploaded=False, rents=rents))
    agents.delete("job1", False, out())
    assert list(fake_vast.instances) == [other] and racing not in fake_vast.instances
    assert unconfirmed not in fake_vast.instances and not core.record_path("job1").exists()


def test_orphans_are_machines_labelled_as_ours_that_no_agent_has(fake_vast):
    kept = fake_vast.add_instance(1, "crab-student-job1-aaaa0000")
    fake_vast.add_instance(1, "crab-student-job2-bbbb0000")  # (a rent of a launch at work)
    orphan = fake_vast.add_instance(1, "crab-student-job3-cccc0000")
    fake_vast.add_instance(1, "my-own-machine")  # (not rented by crab-student)
    core.save(record(instance_id=kept, rent=rent("crab-student-job1-aaaa0000", kept)))
    core.save(record("job2", phase="renting", uploaded=False, rents=[rent("crab-student-job2-bbbb0000")]))
    found = agents.orphans(list(fake_vast.instances.values()), core.all_records())
    assert [i["id"] for i in found] == [orphan] and len(fake_vast.instances) == 4  # (nothing destroyed)


# ---- delete a stopped agent: from its stop note, without starting it ----------------------------


def test_a_stopped_agent_whose_stop_note_shows_nothing_left_is_deleted_without_starting_it(
    fake_vast, fake_github, machines, repo, tmp_path
):
    iid = agent(fake_vast, status="exited", repo=repo)
    push_to_branch(fake_github, tmp_path, NOTE, json.dumps(note()))
    agents.delete("job1", False, out())
    assert iid not in fake_vast.instances and not core.record_path("job1").exists()
    assert not machines.calls and not fake_vast.calls("PUT", "/")  # (not started, nothing asked over ssh)


@pytest.mark.parametrize(
    "left, named",
    [({"outputs": {"files": 2, "bytes": 3.2e9, "sample": ["best.pt", "last.pt"]}}, "best.pt"),
     ({"large_files": [{"path": "data/cache.bin", "bytes": 4.0e9}]}, "data/cache.bin"),
     ({"not_committed": ["notes.txt"], "not_committed_count": 1}, "")],
)  # fmt: skip
def test_a_stop_note_that_lists_work_left_on_the_machine_refuses_the_delete(
    left, named, fake_vast, fake_github, machines, repo, tmp_path
):
    iid = agent(fake_vast, status="exited", repo=repo)
    push_to_branch(fake_github, tmp_path, NOTE, json.dumps(note(**left)))
    message = fails(3, agents.delete, "job1", False, out()).message
    assert named in message and "crab-student start job1" in message  # (start it to copy the work)
    assert iid in fake_vast.instances and core.record_path("job1").exists() and not machines.calls


def test_a_stop_note_from_before_the_last_start_is_not_trusted(
    fake_vast, fake_github, machines, repo, tmp_path
):
    """(A stop that could not push its note leaves the previous stop's on the branch.)"""
    started = time.time() - 3600
    iid = agent(fake_vast, status="exited", repo=repo, last_start=started)
    push_to_branch(fake_github, tmp_path, NOTE, json.dumps(note(stopped_at=int(started) - 600)))
    fails(3, agents.delete, "job1", False, out())
    assert iid in fake_vast.instances and core.record_path("job1").exists() and not machines.calls


@pytest.mark.parametrize("text", [None, "not json", "[]", '{"stopped_at": "a while ago"}'])
def test_a_stopped_agent_without_a_stop_note_to_trust_needs_a_start_or_force(
    text, fake_vast, fake_github, machines, repo, tmp_path
):
    iid = agent(fake_vast, status="exited", repo=repo)
    if text is not None:
        push_to_branch(fake_github, tmp_path, NOTE, text)
    assert "crab-student start job1" in fails(3, agents.delete, "job1", False, out()).message
    assert iid in fake_vast.instances and core.record_path("job1").exists()
    agents.delete("job1", True, out())
    assert not fake_vast.instances and not core.record_path("job1").exists() and not machines.calls


@pytest.mark.parametrize("pushed", [True, False])
def test_an_agent_stopped_with_stop_is_deleted_without_starting_it_once_its_note_is_on_the_branch(
    pushed, fake_vast, fake_github, machines, repo, tmp_path
):
    """Only the branch counts: a save that says it committed the note, but did not push it, leaves nothing to
    trust."""
    iid = agent(fake_vast, repo=repo)
    machines.replies[("job1", "save")] = saved(pushed=pushed)
    agents.stop("job1", out())
    if pushed:  # (the machine's final save pushed its note)
        push_to_branch(fake_github, tmp_path, NOTE, json.dumps(note()))
        agents.delete("job1", False, out())
    else:
        fails(3, agents.delete, "job1", False, out())
    assert (iid in fake_vast.instances) is not pushed and core.record_path("job1").exists() is not pushed
    assert [call[1][0] for call in machines.calls] == ["save"]  # (never started again to check it)
    assert len(fake_vast.calls("PUT", "/")) == 1  # (the stop)


def test_a_start_seen_from_here_makes_the_old_stop_note_stale(
    fake_vast, fake_github, machines, repo, tmp_path
):
    """Started from Vast's website: once list sees it running, the note from before no longer counts."""
    iid = agent(fake_vast, status="exited", repo=repo)
    push_to_branch(fake_github, tmp_path, NOTE, json.dumps(note()))
    agents.views()  # (seen stopped)
    fake_vast.instances[iid].update(actual_status="running", intended_status="running", cur_state="running")
    agents.views()  # (seen running again: it may be doing new work)
    fake_vast.instances[iid].update(actual_status="exited", intended_status="stopped", cur_state="stopped")
    assert core.load("job1")["last_start"] == pytest.approx(time.time(), abs=60)
    fails(3, agents.delete, "job1", False, out())
    assert iid in fake_vast.instances


def test_a_stopped_agent_cannot_be_checked_when_github_cannot_be_asked(fake_vast, machines, monkeypatch):
    iid = agent(fake_vast, status="exited")

    def unreachable(*a):
        raise core.CrabError("GitHub: could not connect")

    monkeypatch.setattr(github, "read_file", unreachable)
    fails(3, agents.delete, "job1", False, out())
    assert iid in fake_vast.instances


# ---- send and report -----------------------------------------------------------------------------


def test_send_types_the_text_through_stdin(fake_vast, machines):
    agent(fake_vast)
    agent(fake_vast, "off", status="exited")
    agents.send("job1", "use the small model")
    assert machines.calls[-1] == ("job1", ["send"], b"use the small model")
    fails(2, agents.send, "job1", "  ")
    fails(1, agents.send, "off", "hello")
    assert [call[1][0] for call in machines.calls] == ["status", "send"]  # (nothing sent to the stopped one)


def test_report_reads_the_report_from_the_branch_on_github(fake_github, repo, tmp_path):
    core.save(record(repo=repo))
    fails(1, agents.report, "job1")  # (not written yet)
    push_to_branch(fake_github, tmp_path, "agent-runs/job1/REPORT.md", "# job1\nIt worked.\n")
    assert "It worked." in agents.report("job1")


def test_send_and_report_of_an_interactive_agent(fake_vast, machines, fake_github, repo):
    agent(fake_vast, mode="interactive", repo=repo)
    machines.replies[("job1", "status")] = status(frontend="vscode", mode="interactive")
    assert "--vscode" in fails(1, agents.send, "job1", "hello").message
    assert "no report" in fails(1, agents.report, "job1").message
    assert [call[1] for call in machines.calls] == [["status"]]


# ---- open ----------------------------------------------------------------------------------------


@pytest.fixture
def opened(monkeypatch) -> list:
    """The names the terminal was opened for."""
    names = []
    monkeypatch.setattr(machine, "open_one", lambda name: names.append(name) or 0)
    monkeypatch.setattr(machine, "open_all", lambda rest: names.extend(rest) or 0)
    return names


def test_open_all_leaves_out_interactive_agents_in_vs_code(fake_vast, machines, opened, capsys):
    for name in ("a", "b", "c"):
        agent(fake_vast, name)
    machines.replies[("a", "status")] = status("working", "x")
    machines.replies[("b", "status")] = status(frontend="vscode", mode="interactive")
    machines.replies[("c", "status")] = status("working", "y")
    assert agents.open_all(["a", "b", "c"]) == 0
    assert opened == ["a", "c"] and "b" in capsys.readouterr().out
    fails(1, agents.open_all, ["b"])


@pytest.mark.parametrize("terminal, handed_off, attached", [(False, True, False), (True, False, False),
                                                          (True, True, True)])  # fmt: skip
def test_open_one_of_an_interactive_agent_opens_vs_code_and_attaches_after_its_hand_off(
    fake_vast, machines, opened, monkeypatch, terminal, handed_off, attached
):
    agent(fake_vast, "a")
    agent(fake_vast, "b", mode="interactive")
    machines.replies[("a", "status")] = status("working", "x")
    machines.replies[("b", "status")] = status(frontend="vscode", mode="interactive")
    vscode = []
    monkeypatch.setattr(machine, "open_vscode", lambda rec, status=None: vscode.append(rec["name"]) or 0)
    monkeypatch.setattr(machine, "interactive", lambda: terminal)
    monkeypatch.setattr(machine, "wait_for_handoff", lambda name: handed_off)
    assert agents.open_one("b") == 0 and vscode == ["b"] and opened == (["b"] if attached else [])
    assert agents.open_one("a") == 0 and vscode == ["b"] and opened[-1] == "a"
