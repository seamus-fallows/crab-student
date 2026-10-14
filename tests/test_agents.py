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
AGENTS = "ada/proj-agents"  # (the agent repo of the user's repo, ada/proj)


def record(name: str = "job1", phase: str = "ready", **fields) -> dict:
    """An agent record with the fields agents read."""
    repo = {"slug": AGENTS, "source": "ada/proj", "branch": f"agent/{name}", "start_sha": None,
            "branch_created": False, "dir": "/root/work/proj"}  # fmt: skip
    return {"name": name, "created": core.now_iso(), "phase": phase, "error": None, "pending": None, "label": None,
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
    """The record's repo part: the agent's branch (at the start commit) on its agent repo, whose deploy key is on
    GitHub and here."""
    bare = fake_github.create_repo(AGENTS, description=f"{github.MARK} for ada/proj")
    sha = git(bare, "rev-parse", "main").strip()
    git(bare, "branch", "agent/job1", sha)
    fake_github.keys[AGENTS] = {
        101: {"title": github.KEY_TITLE, "key": "ssh-ed25519 AAAA", "read_only": "false"}
    }
    github.key_file(AGENTS).parent.mkdir(parents=True, exist_ok=True)
    github.key_file(AGENTS).write_text("private\n")
    Path(f"{github.key_file(AGENTS)}.pub").write_text("ssh-ed25519 AAAA crab-student\n")
    return {"slug": AGENTS, "source": "ada/proj", "branch": "agent/job1", "start_sha": sha,
            "branch_created": True, "dir": "/root/work/proj"}  # fmt: skip


def push_to_branch(fake_github, tmp_path, path: str, text: str) -> None:
    """The agent pushes a file to agent/job1."""
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", "-b", "agent/job1", str(fake_github.bare(AGENTS)), str(work))
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
    ({"phase": "renting"}, None, None, True, "starting", ""),  # (a launch at work)
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


TFA = "Vast wants a fresh 2FA login before it will list your machines."


def vast_down(monkeypatch, why: str = TFA) -> None:
    """Vast cannot be asked (an expired 2FA login, or no connection)."""

    def refuse():
        raise vast.VastError(why, 401)

    monkeypatch.setattr(vast, "instances", refuse)


def test_without_vast_the_machines_own_answer_or_what_was_last_seen_decides():
    seen = {"message": "epoch 3/10", "used_hours": 2.0}
    state, message = agents.display_state(record(last_seen=seen), None, None, False, TFA)
    assert state == "unknown" and message.startswith(TFA) and "epoch 3/10" in message
    done = status("done", "see REPORT")
    assert agents.display_state(record(), None, done, False, TFA) == ("done", "see REPORT")
    stopped = record(last_seen={**seen, "stop_reason": "done"}, stopped_since=time.time() - 7200)
    state, message = agents.display_state(stopped, None, None, False, TFA)
    assert state == "stopped" and "finished" in message


def test_without_vast_machines_are_still_asked_over_ssh_and_the_reason_is_kept(
    fake_vast, machines, monkeypatch
):
    address = {"host": "a.example", "port": 22}
    agent(fake_vast, "a", ssh=address)
    agent(fake_vast, "b", ssh=address, stopped_since=time.time() - 3600, last_seen={"stop_reason": "idle"})
    agent(fake_vast, "c", ssh=address)
    machines.replies[("a", "status")] = status("done", "see REPORT")
    machines.replies[("c", "status")] = status("done", stopping="done", stop_sent_for=400)  # (a stuck stop)
    vast_down(monkeypatch)
    seen, orphans, problem = agents.listing()
    states = {v.name: v.state for v in seen}
    assert problem == TFA and orphans == [] and states == {"a": "done", "b": "stopped", "c": "stopped"}
    assert sorted(call[0] for call in machines.calls) == ["a", "c"]  # (b was last seen stopped: not asked)
    assert core.load("a")["last_seen"]["state"] == "done" and not fake_vast.calls("PUT", "/")


def test_without_vast_an_agent_that_does_not_answer_is_unknown_and_its_record_stays_as_it_was(
    fake_vast, machines, monkeypatch
):
    agent(fake_vast, ssh={"host": "a.example", "port": 22}, last_seen={"state": "working", "used_hours": 1.0})
    machines.replies[("job1", "status")] = machine.Unreachable("could not reach job1 over ssh")
    before = core.load("job1")
    vast_down(monkeypatch)
    v = agents.views()[0]
    assert v.state == "unknown" and TFA in v.message and core.load("job1") == before


# ---- what an agent has cost ----------------------------------------------------------------------


def test_time_and_money_add_up_over_every_start_with_the_disk_of_a_stopped_machine(fake_vast, machines):
    agent(fake_vast, "a", past_hours=2.0)
    machines.replies[("a", "status")] = status("working", "epoch 3/10", used_hours=1.25)
    day_ago = time.time() - 24 * 3600
    agent(fake_vast, "b", status="exited", disk_usd_day=0.24, stopped_hours=24.0, stopped_since=day_ago,
          last_seen={"used_hours": 1.0})  # fmt: skip
    views = {v.name: v for v in agents.views()}
    assert (views["a"].used_h, views["a"].spent) == (3.25, 1.3)
    assert views["b"].used_h == 1.0 and views["b"].spent == pytest.approx(0.4 + 2 * 0.24, abs=0.01)


def test_a_machine_seen_running_again_keeps_what_its_last_session_and_its_stop_cost(fake_vast, machines):
    iid = agent(fake_vast, status="exited", last_seen={"used_hours": 3.0}, stopped_since=time.time() - 7200)
    fake_vast.instances[iid].update(actual_status="running", intended_status="running", cur_state="running")
    machines.replies[("job1", "status")] = status("working", "epoch 1/10", used_hours=0.1)
    assert agents.views()[0].used_h == pytest.approx(3.1)
    rec = core.load("job1")
    assert rec["past_hours"] == 3.0 and rec["stopped_hours"] == pytest.approx(2.0, abs=0.01)
    agents.views()
    assert core.load("job1")["past_hours"] == 3.0  # (counted once)


def test_a_machine_whose_own_clock_went_back_was_stopped_and_started_unseen(fake_vast, machines):
    started = time.time() - 5 * 3600
    agent(fake_vast, last_seen={"used_hours": 3.0}, last_start=started)
    machines.replies[("job1", "status")] = status("working", "epoch 1/10", used_hours=0.2)
    assert agents.views()[0].used_h == pytest.approx(3.2)
    rec = core.load("job1")
    assert rec["past_hours"] == 3.0 and rec["last_start"] > started  # (a stop note from before is stale now)


def test_two_looks_that_save_out_of_order_do_not_count_the_hours_twice(fake_vast, machines):
    """(A wait in the background and a list: the one that asked the machine first may save last.)"""
    agent(fake_vast, last_seen={"used_hours": 10.05})  # (saved by the look that asked second)
    machines.replies[("job1", "status")] = status("working", "epoch 9/10", used_hours=10.0)
    assert agents.views()[0].used_h == pytest.approx(10.0) and not core.load("job1").get("past_hours")


def test_a_deleted_agents_cost_is_kept_for_spend(fake_vast, machines):
    agent(fake_vast, past_hours=1.0, last_seen={"used_hours": 0.5})
    agent(fake_vast, "job2")
    core.save(record("job3", phase="failed", dph=None))  # (it never had a machine)
    machines.replies[("job1", "check")] = CLEAN
    machines.replies[("job2", "status")] = status("working", "epoch 3/10", used_hours=2.0)
    agents.delete("job1", False, out())
    agents.delete("job3", False, out())
    res = agents.spending()
    assert [(d["name"], d["hours"], d["usd"]) for d in res["deleted"]] == [("job1", 1.5, 0.6)]
    assert [(a["name"], a["usd"]) for a in res["agents"]] == [("job2", 0.8)] and res["total_usd"] == 1.4


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


def test_a_launch_whose_setup_failed_has_nothing_to_stop(fake_vast):
    core.save(record(phase="failed", uploaded=True, instance_id=None))  # (its machine was destroyed)
    fails(1, agents.stop, "job1", out())


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
def test_a_clean_check_destroys_the_machine_and_keeps_the_branch_and_the_deploy_key(
    worked, fake_vast, fake_github, machines, repo, tmp_path
):
    agent(fake_vast, repo=repo)
    if worked:
        push_to_branch(fake_github, tmp_path, "result.txt", "42\n")
    branch = fake_github.branch_sha(AGENTS, "agent/job1")
    machines.replies[("job1", "check")] = CLEAN
    agents.delete("job1", False, out())
    assert [call[1] for call in machines.calls] == [["check"]]
    assert not fake_vast.instances and not core.record_path("job1").exists()
    assert fake_github.branch_sha(AGENTS, "agent/job1") == branch  # (the agent repo keeps the agents' work)
    assert list(fake_github.keys[AGENTS]) == [101] and github.key_file(AGENTS).exists()  # (for the next one)
    assert not [c for c in fake_github.calls if c[:2] == ("api", "-X") and c[2] != "GET"]  # (nothing written)


@pytest.mark.parametrize("confirmed", [True, False])
def test_delete_destroys_the_machine_of_a_launch_cut_off_before_its_upload(confirmed, fake_vast):
    """Its id is in the record, or only its rent's label (Vast did not confirm the rent)."""
    booting = fake_vast.add_instance(1, "cs-job1-abc123-aaaa", status="loading")
    other = fake_vast.add_instance(1, "cs-job1-abc123-bbbb", status="loading")  # (not this launch's label)
    iid = booting if confirmed else None
    core.save(record(phase="renting", uploaded=False, label="cs-job1-abc123-aaaa", instance_id=iid))
    res = agents.delete("job1", False, out())
    assert list(fake_vast.instances) == [other] and not core.record_path("job1").exists()
    assert "cs-job1-abc123-aaaa" not in res["result_line"]  # (no machine to look out for)


def test_delete_of_a_launch_whose_unconfirmed_rent_left_no_machine_names_its_label(fake_vast):
    other = fake_vast.add_instance(1, "cs-job1-abc123-bbbb", status="loading")
    core.save(record(phase="renting", uploaded=False, label="cs-job1-abc123-aaaa"))
    res = agents.delete("job1", False, out())
    assert list(fake_vast.instances) == [other] and not core.record_path("job1").exists()
    assert "cs-job1-abc123-aaaa" in res["result_line"]  # (the machine to look out for)


def test_orphans_are_machines_labelled_as_ours_that_no_agent_has(fake_vast):
    core.home().mkdir(parents=True, exist_ok=True)
    (core.home() / "install_id").write_text("abc123")  # (this computer's id in its rents' labels)
    kept = fake_vast.add_instance(1, "SFALLOWS-job1-abc123-aaaa")
    fake_vast.add_instance(
        1, "crab-student-job2-abc123-bbbb"
    )  # (the rent of a launch at work, not confirmed yet)
    orphans = [
        fake_vast.add_instance(1, label) for label in ("SFALLOWS-job3-abc123-cccc", "x-job4-abc123-dddd")
    ]
    fake_vast.add_instance(1, "SFALLOWS-job3-def456-cccc")  # (a teammate's, on a shared Vast account)
    fake_vast.add_instance(1, "SFALLOWS")  # (not rented by crab-student)
    core.save(record(instance_id=kept, label="SFALLOWS-job1-abc123-aaaa"))
    core.save(record("job2", phase="renting", uploaded=False, label="crab-student-job2-abc123-bbbb"))
    found = agents.orphans(list(fake_vast.instances.values()), core.all_records())
    assert [i["id"] for i in found] == orphans and len(fake_vast.instances) == 6  # (nothing destroyed)


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


def test_list_while_vast_stops_the_machine_shows_it_stopped_and_keeps_its_stop_note_good(
    fake_vast, fake_github, machines, repo, tmp_path
):
    """Vast takes a while to stop a machine: on its way down it was not started again."""
    iid = agent(fake_vast, repo=repo)
    started = core.load("job1")["last_start"]
    machines.replies[("job1", "save")] = saved()
    agents.stop("job1", out())
    push_to_branch(fake_github, tmp_path, NOTE, json.dumps(note()))  # (the stop's final save pushed it)
    fake_vast.instances[iid]["actual_status"] = "running"  # (still running, Vast's intended status: stopped)
    assert agents.views()[0].state == "stopped" and core.load("job1")["last_start"] == started
    agents.delete("job1", False, out())  # (the note counts: no start, no --force)
    assert iid not in fake_vast.instances and not core.record_path("job1").exists()


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


# ---- fetch, and delete after it ------------------------------------------------------------------

OUTPUTS = {
    "files": 3,
    "bytes": 5_100_000_000,
    "sample": ["best.pt"],
}  # (what the machine's check says it holds)
SENT = {"files": 3, "bytes": 5_100_000_000, "digest": "d1"}  # (the machine's account of what it sent)
FETCHED = {**SENT, "to": "C:/results", "at": "2026-10-10T14:30:00Z"}  # (what the record keeps of a copy)
ADDRESS = {"host": "a.example", "port": 22}


def copying(monkeypatch, write: int = 3, **got) -> list:
    """machine.fetch_outputs replaced: the (name, folder) of each copy. It writes `write` files and answers
    with a complete copy of what the machine sent (SENT), changed by `got`; an exception in `got` is raised
    after the files are written."""
    calls = []

    def fetch_outputs(name, dest, on_progress):
        calls.append((name, dest))
        for i in range(write):
            (dest / f"{i}.pt").write_bytes(b"x")
        on_progress(write, 1000)
        if isinstance(got.get("raises"), Exception):
            raise got["raises"]
        return {"files": 3, "bytes": 5_100_000_000, "sent": dict(SENT), "refused": [], **got}

    monkeypatch.setattr(machine, "fetch_outputs", fetch_outputs)
    return calls


def test_fetch_copies_the_outputs_into_a_new_folder_and_the_record_keeps_what_was_copied(
    monkeypatch, tmp_path
):
    core.save(record(ssh=ADDRESS))
    calls = copying(monkeypatch)
    dest = (tmp_path / "results").resolve()
    res = agents.fetch("job1", tmp_path / "results", out())
    kept = core.load("job1")["fetched"]
    assert calls == [("job1", dest)] and res["files"] == 3 and len(list(dest.iterdir())) == 3
    assert {k: kept[k] for k in SENT} == SENT and kept["to"] == str(dest)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "job1-outputs").mkdir()  # (an empty folder is as good as none)
    agents.fetch("job1", None, out())
    agents.fetch("job1", None, out())  # (again later: the first copy stays, the new one goes beside it)
    assert [c[1].name for c in calls[1:]] == ["job1-outputs", "job1-outputs-2"]
    assert all(len(list((tmp_path / name).iterdir())) == 3 for name in ("job1-outputs", "job1-outputs-2"))
    assert core.load("job1")["fetched"]["to"] == str((tmp_path / "job1-outputs-2").resolve())


def test_fetch_never_writes_into_a_folder_that_holds_something_or_into_crab_students_own(
    monkeypatch, tmp_path
):
    """(The machine chooses the names: in a repo they could be .git/hooks, in ~/.crab-student the settings.)"""
    core.save(record(ssh=ADDRESS))
    calls = copying(monkeypatch)
    (tmp_path / "mine.txt").write_text("keep me\n")
    a_file = tmp_path / "mine.txt"
    for dest in (tmp_path, a_file, core.home(), core.home() / "new-folder"):
        fails(2, agents.fetch, "job1", dest, out())
    assert not calls and a_file.read_text() == "keep me\n" and not core.load("job1").get("fetched")


@pytest.mark.parametrize(
    "got", [{"sent": {**SENT, "files": 4}}, {"files": 2}, {"refused": [("a:b.txt", "not a name")]}]
)
def test_a_copy_that_is_not_complete_fails_and_is_not_kept_in_the_record(got, monkeypatch, tmp_path):
    core.save(record(ssh=ADDRESS))
    copying(monkeypatch, **got)
    fails(1, agents.fetch, "job1", tmp_path / "results", out())
    assert not core.load("job1").get("fetched") and (tmp_path / "results").exists()  # (what arrived stays)


def test_a_copy_that_is_cut_off_leaves_nothing_behind_so_that_it_can_be_run_again(monkeypatch, tmp_path):
    core.save(record(ssh=ADDRESS))
    copying(monkeypatch, write=2, raises=core.CrabError("the copy from job1 was cut off (unexpected end)."))
    fails(1, agents.fetch, "job1", tmp_path / "results", out())
    assert not (tmp_path / "results").exists() and not core.load("job1").get("fetched")
    copying(monkeypatch)
    agents.fetch("job1", tmp_path / "results", out())
    assert core.load("job1")["fetched"]["digest"] == "d1"


def test_fetch_needs_a_machine_that_answers(monkeypatch, tmp_path):
    core.save(record())  # (it never had an address)
    fails(1, agents.fetch, "job1", tmp_path / "results", out())
    core.save(record(ssh=ADDRESS))
    copying(monkeypatch, write=0, raises=machine.Unreachable("could not reach job1 over ssh (timed out)."))
    assert "crab-student start job1" in fails(1, agents.fetch, "job1", tmp_path / "results", out()).message


def test_fetch_of_an_agent_without_outputs_keeps_no_record_and_no_folder(monkeypatch, tmp_path):
    core.save(record(ssh=ADDRESS))
    copying(monkeypatch, write=0, files=0, bytes=0, sent={"files": 0, "bytes": 0, "digest": "d0"})
    agents.fetch("job1", tmp_path / "results", out())
    assert not (tmp_path / "results").exists() and not core.load("job1").get("fetched")


def outputs_now(monkeypatch, answer) -> list:
    """machine.outputs_state replaced: what the machine says its /root/outputs is now (or an error)."""
    asked = []

    def outputs_state(name):
        asked.append(name)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(machine, "outputs_state", outputs_state)
    return asked


@pytest.mark.parametrize(
    "fetched, now, goes",
    [
        (FETCHED, SENT, True),
        (FETCHED, {**SENT, "digest": "d2"}, False),  # (as many files and bytes, but not the same ones)
        (FETCHED, machine.Unreachable("could not reach job1 over ssh"), False),
        (None, SENT, False),
    ],
)
def test_delete_lets_go_of_outputs_only_while_every_file_is_as_it_was_fetched(
    fetched, now, goes, fake_vast, machines, monkeypatch
):
    iid = agent(fake_vast, fetched=fetched)
    machines.replies[("job1", "check")] = {**CLEAN, "outputs": OUTPUTS}
    asked = outputs_now(monkeypatch, now)
    if goes:
        agents.delete("job1", False, out())
    else:
        assert "crab-student fetch job1" in fails(3, agents.delete, "job1", False, out()).message
    assert (iid in fake_vast.instances) is not goes and asked == (["job1"] if fetched else [])


@pytest.mark.parametrize("noted, goes", [("d1", True), ("d2", False), (None, False)])
def test_a_stopped_agents_outputs_go_only_when_its_stop_note_says_they_are_as_fetched(
    noted, goes, fake_vast, fake_github, machines, repo, tmp_path, monkeypatch
):
    """(None: a machine set up before stop notes carried the fingerprint cannot say.)"""
    iid = agent(fake_vast, status="exited", repo=repo, fetched=FETCHED)
    left = {**OUTPUTS, "digest": noted} if noted else OUTPUTS
    push_to_branch(fake_github, tmp_path, NOTE, json.dumps(note(outputs=left)))
    asked = outputs_now(monkeypatch, SENT)
    if goes:
        agents.delete("job1", False, out())
    else:
        assert FETCHED["to"] in fails(3, agents.delete, "job1", False, out()).message  # (where the copy is)
    assert (iid in fake_vast.instances) is not goes and not asked and not machines.calls  # (never started)


# ---- wait ----------------------------------------------------------------------------------------


def seen(name: str, state: str, message: str = "") -> agents.View:
    return agents.View(name=name, state=state, message=message, used_h=1.0, spent=0.4, idle_min=0, machine="",
                       dph=0.4, branch=f"agent/{name}", repo="ada/proj", git=None, vast_status=None, pending=None,
                       error=None, stopped_h=None, disk_usd_day=None, mode="colleague", frontend="tmux",
                       settings={})  # fmt: skip


def looks(monkeypatch, *rounds, problem: str | None = None) -> list:
    """agents.listing replaced: each look answers with the next of `rounds` ({name: state}), the last one from
    then on. Returns the names each look asked for. wait looks again at once."""
    asked: list = []

    def listing(names=None):
        asked.append(names)
        states = rounds[min(len(asked), len(rounds)) - 1]
        return [seen(n, s) for n, s in states.items() if names is None or n in names], [], problem

    monkeypatch.setattr(agents, "listing", listing)
    monkeypatch.setattr(agents, "WAIT_POLL", 0.01)
    for name in {n for states in rounds for n in states}:
        core.save(record(name))
    return asked


def test_wait_ends_when_an_agent_needs_the_user(monkeypatch):
    working = {"a": "working", "b": "starting"}
    asked = looks(monkeypatch, working, working, {"a": "done", "b": "working"})
    res = agents.wait(["a", "b"], 60)
    assert [v["name"] for v in res["attention"]] == ["a"] and res["waiting"] == ["b"]
    assert (res["exit_code"], res["timed_out"]) == (0, False) and "a: done" in res["result_line"]
    assert asked == [["a", "b"]] * 3


def test_wait_without_names_watches_every_agent_so_one_that_finished_meanwhile_is_not_passed_over(
    monkeypatch,
):
    """(After a wait that ended for a, the lead looks at a's results; b finishes meanwhile.)"""
    asked = looks(monkeypatch, {"a": "done", "b": "done", "c": "working"})
    res = agents.wait([], 60)
    assert [v["name"] for v in res["attention"]] == ["a", "b"] and res["waiting"] == ["c"] and asked == [None]
    asked = looks(monkeypatch, {"a": "working", "b": "working"}, {"a": "working", "b": "failed"})
    assert [v["name"] for v in agents.wait([], 60)["attention"]] == ["b"] and asked == [None, ["a", "b"]]
    looks(monkeypatch, {})
    fails(1, agents.wait, [], 60)  # (there is no agent)


def test_wait_keeps_saying_that_the_vast_login_ended_until_there_is_a_new_one(monkeypatch):
    """(Vast may answer a list with the plain key again once the ended session's key is gone.)"""
    ended = f"Vast wants a fresh 2FA login. Run: {vast.TFA_LOGIN}"
    rounds = [({"a": "working"}, ended), ({"a": "working"}, None), ({"a": "done"}, None)]
    asked = []

    def listing(names=None):
        asked.append(names)
        states, problem = rounds[min(len(asked), len(rounds)) - 1]
        return [seen(n, s) for n, s in states.items()], [], problem

    monkeypatch.setattr(agents, "listing", listing)
    monkeypatch.setattr(agents, "WAIT_POLL", 0.01)
    core.save(record("a"))
    assert agents.wait(["a"], 60)["vast_problem"] == ended
    core.write_private(core.vast_session_file(), "a-new-session\n")  # (the user logged in again meanwhile)
    asked.clear()
    assert agents.wait(["a"], 60)["vast_problem"] is None


def test_wait_on_an_agent_that_already_needs_the_user_ends_at_once(monkeypatch):
    asked = looks(monkeypatch, {"a": "needs you"})
    assert agents.wait(["a"], 60)["attention"][0]["state"] == "needs you" and len(asked) == 1
    fails(1, agents.wait, ["nobody"], 60)  # (no such agent)


def test_wait_comes_back_when_the_time_is_up(monkeypatch):
    looks(monkeypatch, {"a": "working"})
    res = agents.wait(["a"], 0.05)
    assert (res["timed_out"], res["exit_code"], res["attention"], res["waiting"]) == (True, 0, [], ["a"])


def test_wait_gives_up_on_an_agent_nobody_can_see_and_says_why(monkeypatch):
    monkeypatch.setattr(agents, "UNSEEN_LIMIT", 0.05)
    looks(monkeypatch, {"a": "unknown", "b": "working"}, problem=TFA)
    res = agents.wait(["a", "b"], 60)
    assert res["exit_code"] == 1 and [v["name"] for v in res["unseen"]] == ["a"] and res["waiting"] == ["b"]
    assert res["vast_problem"] == TFA
    blip = [{"a": "unknown"}, {"a": "working"}, {"a": "unknown"}, {"a": "done"}]
    looks(monkeypatch, *blip)
    assert agents.wait(["a"], 60)["exit_code"] == 0  # (seen again in between: no alarm)


def test_wait_says_when_an_agent_was_deleted_meanwhile(monkeypatch):
    looks(monkeypatch, {"a": "working", "b": "working"}, {"b": "working"})
    res = agents.wait(["a", "b"], 60)
    assert res["deleted"] == ["a"] and res["exit_code"] == 0 and res["waiting"] == ["b"]
