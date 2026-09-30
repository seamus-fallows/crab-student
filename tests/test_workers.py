"""Workers once launched: display states, views, stop, delete, send and report. Records are made by hand;
Vast, GitHub and the machines are the fakes."""

from __future__ import annotations

import io

import pytest
from fake_gh import git

from crab_student import core, launch, machine, vast, workers

RUNNING = {"id": 7, "actual_status": "running", "intended_status": "running"}
STOPPED = {"id": 7, "actual_status": "exited", "intended_status": "stopped"}
BOOTING = {"id": 7, "actual_status": "loading", "intended_status": "running", "status_msg": "pulling image"}
CLEAN = {"committed": 0, "pushed": True, "dirty": 0, "unpushed": 0, "error": None,
         "outputs": {"files": 0, "bytes": 0, "sample": []}, "large_files": []}  # fmt: skip


def record(name: str = "job1", phase: str = "ready", **fields) -> dict:
    """A worker record with the fields workers read."""
    repo = {"slug": "ada/proj", "branch": f"agent/{name}", "start_sha": None, "deploy_key_id": None,
            "branch_created": False, "dir": "/root/work/proj"}  # fmt: skip
    return {"name": name, "created": core.now_iso(), "phase": phase, "error": None, "pending": None, "rent": None,
            "instance_id": None, "dph": 0.40, "budget_usd": 2.0, "limit_hours": 5.0, "machine": "1x RTX 4090",
            "ssh": None, "last_seen": None, "repo": repo, **fields}  # fmt: skip


def worker(fake_vast, name: str = "job1", status: str = "running", **fields) -> int:
    """A ready worker whose machine is running or stopped ("exited") on the fake Vast: its instance id."""
    down = {} if status == "running" else {"intended_status": "stopped", "cur_state": "stopped"}
    iid = fake_vast.add_instance(1, f"cs:{name}:0", status=status, **down)
    core.save(record(name, instance_id=iid, **fields))
    return iid


def status(state: str | None = None, message: str = "", **fields) -> dict:
    """What cs.py status answers."""
    return {"worker": {"state": state, "message": message} if state else {}, "idle_seconds": 0, "used_hours": 1.0,
            "stopping": None, "stop_reason": None, "git": {"dirty": 0, "unpushed": 0}, **fields}  # fmt: skip


def out() -> launch.Progress:
    return launch.Progress(io.StringIO())


def fails(code: int, fn, *a) -> core.CrabError:
    with pytest.raises(core.CrabError) as e:
        fn(*a)
    assert e.value.code == code, e.value.message
    return e.value


@pytest.fixture
def repo(fake_github) -> dict:
    """The record's repo part, with the worker's deploy key and its branch (at the start commit) on GitHub."""
    bare = fake_github.create_repo("ada/proj")
    sha = git(bare, "rev-parse", "main").strip()
    git(bare, "branch", "agent/job1", sha)
    fake_github.keys["ada/proj"] = {101: {"title": "crab-student job1"}}
    return {"slug": "ada/proj", "branch": "agent/job1", "start_sha": sha, "deploy_key_id": 101,
            "branch_created": True, "dir": "/root/work/proj"}  # fmt: skip


def push_to_branch(fake_github, tmp_path, path: str, text: str) -> None:
    """The worker pushes a file to agent/job1."""
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", "-b", "agent/job1", str(fake_github.bare("ada/proj")), str(work))
    (work / path).parent.mkdir(parents=True, exist_ok=True)
    (work / path).write_text(text)
    git(work, "add", path)
    git(work, "commit", "-q", "-m", "work")
    git(work, "push", "-q", "origin", "agent/job1")


# ---- what list and status show -------------------------------------------------------------------

# fmt: off
DISPLAY = [  # (record fields, Vast's instance, cs.py status, lock held) -> STATE, and a part of MESSAGE
    ({"phase": "pending", "pending": {"action": "launch", "reason": "budget $84.00"}}, None, None, False,
     "approval", "$84.00"),
    ({"phase": "booting"}, None, None, True, "launching", ""),
    ({"phase": "renting", "rent": {"label": "cs:job1:0"}}, None, None, False, "rent unknown", "start job1"),
    ({"phase": "setup"}, None, None, False, "interrupted", "start job1"),
    ({"phase": "failed", "error": "every offer failed"}, None, None, False, "launch failed", "every offer failed"),
    ({}, None, None, False, "gone", ""),
    ({}, BOOTING, None, False, "starting", "pulling image"),
    ({"last_seen": {"stop_reason": "idle", "message": "sweep 4/6"}}, STOPPED, None, False, "stopped", "sweep 4/6"),
    ({"pending": {"action": "start", "reason": "5 machines"}}, STOPPED, None, False, "stopped", "start"),
    ({"last_seen": {"message": "epoch 3/10"}}, RUNNING, None, False, "unreachable", "epoch 3/10"),
    ({}, RUNNING, status("working", stopping=True, stop_reason="idle"), False, "stopping", ""),
    ({}, RUNNING, status("needs-you", "Drop the old table?"), False, "needs you", "Drop the old table?"),
    ({}, RUNNING, status("done", "see REPORT", git={"dirty": 0, "unpushed": 1}), False, "done*", "see REPORT"),
    ({}, RUNNING, status("done", "see REPORT"), False, "done", "see REPORT"),
    ({}, RUNNING, status("failed", "out of memory"), False, "failed", "out of memory"),
    ({}, RUNNING, status(), False, "starting", ""),
    ({}, RUNNING, status("working", "epoch 3/10", idle_seconds=1380), False, "idle", "epoch 3/10"),
    ({}, RUNNING, status("working", "epoch 3/10", idle_seconds=120), False, "working", "epoch 3/10"),
]
# fmt: on


@pytest.mark.parametrize("fields,inst,st,busy,state,shown", DISPLAY)
def test_display_state(fields, inst, st, busy, state, shown):
    got, message = workers.display_state(record(**fields), inst, st, busy)
    assert got == state and shown in message


def test_display_state_without_vast():
    rec = record(last_seen={"message": "epoch 3/10"})
    assert workers.display_state(rec, None, None, False, vast_down=True)[0] == "unknown"


def test_views_use_live_status_and_remember_what_they_saw(fake_vast, machines):
    worker(fake_vast, "a")
    b = worker(fake_vast, "b", ssh={"host": "old.example", "port": 22})  # (its port moved since)
    last = {"stop_reason": "user", "message": "half done", "used_hours": 1.5}
    worker(fake_vast, "c", status="exited", last_seen=last)
    machines.replies[("a", "status")] = status("working", "epoch 3/10", used_hours=1.25)
    machines.replies[("b", "status")] = machine.Unreachable("could not reach b over ssh")
    views = {v.name: v for v in workers.views()}
    a = views["a"]
    assert (a.state, a.message, a.used_h, a.spent) == ("working", "epoch 3/10", 1.25, 0.5)
    assert views["b"].state == "unreachable"
    assert views["c"].state == "stopped" and "half done" in views["c"].message and views["c"].used_h == 1.5
    assert {call[0] for call in machines.calls} == {"a", "b"}  # (no ssh to a stopped machine)
    seen = core.load("a")["last_seen"]
    assert (seen["state"], seen["message"], seen["used_hours"]) == ("working", "epoch 3/10", 1.25)
    host, port = vast.address(fake_vast.instances[b])
    assert core.load("b")["ssh"] == {"host": host, "port": port}
    assert f"Port {port}" in machine.config_file().read_text()


# ---- stop ----------------------------------------------------------------------------------------


def test_stop_saves_while_the_machine_still_runs_then_stops_it(fake_vast, monkeypatch):
    iid = worker(fake_vast)
    saves = []  # (the command, and the machine's state while it ran)

    def remote(name, args, **kw):
        saves.append((args[0], fake_vast.instances[iid]["actual_status"]))
        return {"committed": 2, "pushed": True, "error": None}

    monkeypatch.setattr(machine, "remote", remote)
    workers.stop("job1", out())
    assert saves == [("save", "running")] and fake_vast.instances[iid]["intended_status"] == "stopped"
    assert core.load("job1")["last_seen"]["stop_reason"] == "user"


@pytest.mark.parametrize(
    "error", [machine.Unreachable("could not reach job1"), core.CrabError("job1: git broke")]
)
def test_stop_still_stops_when_the_save_fails(error, fake_vast, machines):
    iid = worker(fake_vast)
    machines.replies[("job1", "save")] = error
    workers.stop("job1", out())
    assert fake_vast.instances[iid]["intended_status"] == "stopped"


def test_stopping_a_stopped_worker_changes_nothing(fake_vast, machines):
    worker(fake_vast, status="exited")
    workers.stop("job1", out())
    assert not fake_vast.calls("PUT", "/") and not machines.calls


# ---- delete --------------------------------------------------------------------------------------


def test_deleting_a_pending_worker_only_forgets_it(fake_vast):
    core.save(record(phase="pending", pending={"action": "launch", "reason": "budget $84.00"}))
    workers.delete("job1", False, out())
    assert not core.record_path("job1").exists() and not fake_vast.requests


def test_a_stopped_worker_cannot_be_checked_so_delete_needs_force(fake_vast, machines):
    iid = worker(fake_vast, status="exited")
    fails(3, workers.delete, "job1", False, out())
    assert iid in fake_vast.instances and core.record_path("job1").exists()
    workers.delete("job1", True, out())
    assert not fake_vast.instances and not core.record_path("job1").exists() and not machines.calls


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
    iid = worker(fake_vast)
    machines.replies[("job1", "check")] = {**CLEAN, **problem}
    assert named in fails(3, workers.delete, "job1", False, out()).message
    assert iid in fake_vast.instances and core.record_path("job1").exists()


@pytest.mark.parametrize("worked", [False, True])
def test_a_clean_check_destroys_the_machine_and_keeps_only_a_branch_with_work(
    worked, fake_vast, fake_github, machines, repo, tmp_path
):
    worker(fake_vast, repo=repo)
    if worked:
        push_to_branch(fake_github, tmp_path, "result.txt", "42\n")
    machines.replies[("job1", "check")] = CLEAN
    workers.delete("job1", False, out())
    assert [call[1] for call in machines.calls] == [["check"]]
    assert not fake_vast.instances and not core.record_path("job1").exists()
    assert fake_github.keys["ada/proj"] == {}  # (the deploy key is gone)
    assert (fake_github.branch_sha("ada/proj", "agent/job1") is not None) is worked


def test_delete_destroys_the_machine_of_an_unconfirmed_rent_found_by_its_label(fake_vast):
    iid = fake_vast.add_instance(1, "cs:job1:abcd1234", status="loading")
    fake_vast.add_instance(1, "cs:job1:00000000", status="loading")  # (not this rent's label: left alone)
    core.save(record(phase="renting", rent={"label": "cs:job1:abcd1234", "offer_id": 1, "at": 0}))
    workers.delete("job1", False, out())
    assert iid not in fake_vast.instances and len(fake_vast.instances) == 1
    assert not core.record_path("job1").exists()


# ---- send and report -----------------------------------------------------------------------------


def test_send_types_the_text_through_stdin(fake_vast, machines):
    worker(fake_vast)
    worker(fake_vast, "off", status="exited")
    workers.send("job1", "use the small model")
    assert machines.calls == [("job1", ["send"], b"use the small model")]
    fails(2, workers.send, "job1", "  ")
    fails(1, workers.send, "off", "hello")
    assert len(machines.calls) == 1


def test_report_reads_the_report_from_the_branch_on_github(fake_github, repo, tmp_path):
    core.save(record(repo=repo))
    fails(1, workers.report, "job1")  # (not written yet)
    push_to_branch(fake_github, tmp_path, "agent-runs/job1/REPORT.md", "# job1\nIt worked.\n")
    assert "It worked." in workers.report("job1")
