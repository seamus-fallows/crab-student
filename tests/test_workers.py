"""Workers once launched: display states, views, stop, delete (a stopped one from its stop note), send and
report. Records are made by hand; Vast, GitHub and the machines are the fakes."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest
from fake_gh import git

from crab_student import core, github, launch, machine, vast, workers

RUNNING = {"id": 7, "actual_status": "running", "intended_status": "running"}
STOPPED = {"id": 7, "actual_status": "exited", "intended_status": "stopped"}
BOOTING = {"id": 7, "actual_status": "loading", "intended_status": "running", "status_msg": "pulling image"}
CLEAN = {"committed": 0, "pushed": True, "dirty": 0, "unpushed": 0, "error": None,
         "outputs": {"files": 0, "bytes": 0, "sample": []}, "large_files": []}  # fmt: skip
NOTE = "agent-runs/job1/stopped.json"  # where a machine that stopped itself leaves its stop note


def record(name: str = "job1", phase: str = "ready", **fields) -> dict:
    """A worker record with the fields workers read."""
    repo = {"slug": "ada/proj", "branch": f"agent/{name}", "start_sha": None, "deploy_key": None,
            "branch_created": False, "dir": "/root/work/proj"}  # fmt: skip
    return {"name": name, "created": core.now_iso(), "phase": phase, "error": None, "pending": None, "rent": None,
            "instance_id": None, "dph": 0.40, "disk_usd_day": None, "limit_hours": 5.0, "machine": "1x RTX 4090",
            "ssh": None, "last_seen": None, "last_start": None, "stopped_since": None, "stop_note": None,
            "repo": repo, "uploaded": phase == "ready", **fields}  # fmt: skip


def worker(fake_vast, name: str = "job1", status: str = "running", **fields) -> int:
    """A ready worker whose machine (rented an hour ago) is running or stopped ("exited") on the fake Vast: its
    instance id."""
    down = {} if status == "running" else {"intended_status": "stopped", "cur_state": "stopped"}
    iid = fake_vast.add_instance(1, f"cs:{name}:0", status=status, **down)
    core.save(record(name, instance_id=iid, **{"last_start": time.time() - 3600, **fields}))
    return iid


def status(state: str | None = None, message: str = "", **fields) -> dict:
    """What cs.py status answers."""
    return {"worker": {"state": state, "message": message} if state else {}, "idle_seconds": 0, "used_hours": 1.0,
            "stopping": None, "stop_reason": None, "git": {"dirty": 0, "unpushed": 0}, **fields}  # fmt: skip


def note(**fields) -> dict:
    """A stop note as the machine leaves it (on the branch, and in the answer of its final save): written a
    minute ago, nothing left behind."""
    return {"stopped_at": int(time.time()) - 60, "reason": "idle", "outputs": {"files": 0, "bytes": 0, "sample": []},
            "large_files": [], "not_committed": [], "not_committed_count": 0, **fields}  # fmt: skip


def saved(**fields) -> dict:
    """What cs.py save --final answers: committed and pushed, the stop note in the commit."""
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
    """The record's repo part, with the worker's deploy key and its branch (at the start commit) on GitHub."""
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
    ({"phase": "pending", "pending": {"reason": "it would cost $6.00/hr", "max_dph": 6.0}}, None, None, False,
     "approval", "$6.00/hr"),
    ({"phase": "booting"}, None, None, True, "starting", ""),  # (a launch at work)
    ({"phase": "renting", "rent": {"label": "cs:job1:0"}}, None, None, False, "interrupted", "start job1"),
    ({"phase": "renting"}, None, None, False, "interrupted", "start job1"),
    ({"phase": "setup"}, None, None, False, "interrupted", "start job1"),
    ({"phase": "failed", "error": "every offer failed"}, None, None, False, "failed", "every offer failed"),
    ({}, None, None, False, "failed", "delete job1"),  # (the machine no longer exists on Vast)
    ({}, BOOTING, None, False, "starting", "pulling image"),
    ({"last_seen": {"stop_reason": "idle", "message": "sweep 4/6"}}, STOPPED, None, False, "stopped", "sweep 4/6"),
    ({"last_seen": {"used_hours": 2.0}}, STOPPED, None, False, "stopped", "stopped itself"),  # (idle or done)
    ({"last_seen": {"used_hours": 5.0}}, STOPPED, None, False, "stopped", "time limit"),
    ({"last_seen": {"message": "epoch 3/10"}}, RUNNING, None, False, "unknown", "epoch 3/10"),  # (no answer)
    ({}, RUNNING, status("working", stopping=True, stop_reason="idle"), False, "stopped", "stopping"),
    ({}, RUNNING, status("needs-you", "Drop the old table?"), False, "needs you", "Drop the old table?"),
    ({}, RUNNING, status("done", "see REPORT", git={"dirty": 0, "unpushed": 1}), False, "done", "pushed"),
    ({}, RUNNING, status("done", "see REPORT"), False, "done", "see REPORT"),
    ({}, RUNNING, status("failed", "out of memory"), False, "failed", "out of memory"),
    ({}, RUNNING, status(), False, "starting", ""),  # (the worker has not said anything yet)
    ({}, RUNNING, status("working", "epoch 3/10", idle_seconds=1380), False, "working", "idle 23m"),
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
    assert views["b"].state == "unknown"
    assert views["c"].state == "stopped" and "half done" in views["c"].message and views["c"].used_h == 1.5
    assert {call[0] for call in machines.calls} == {"a", "b"}  # (no ssh to a stopped machine)
    seen = core.load("a")["last_seen"]
    assert (seen["state"], seen["message"], seen["used_hours"]) == ("working", "epoch 3/10", 1.25)
    host, port = vast.address(fake_vast.instances[b])
    assert core.load("b")["ssh"] == {"host": host, "port": port}
    assert f"Port {port}" in machine.config_file().read_text()


def test_views_say_how_long_a_machine_has_been_stopped_and_what_its_disk_costs(fake_vast, machines):
    iid = worker(fake_vast, status="exited", disk_usd_day=0.17)
    v = workers.views()[0]
    since = core.load("job1")["stopped_since"]
    assert v.state == "stopped" and since and v.stopped_h < 0.1 and v.disk_usd_day == 0.17
    core.save({**core.load("job1"), "stopped_since": since - 3 * 3600})  # (first seen stopped 3 hours ago)
    v = workers.views()[0]
    assert 2.9 < v.stopped_h < 3.1 and core.fmt_hours(3) in v.message and core.usd(0.17) in v.message
    assert core.load("job1")["stopped_since"] == since - 3 * 3600  # (still counted from then)
    fake_vast.instances[iid].update(actual_status="running", intended_status="running", cur_state="running")
    machines.replies[("job1", "status")] = status("working", "epoch 3/10")
    v = workers.views()[0]
    assert v.state == "working" and v.stopped_h is None and core.load("job1")["stopped_since"] is None


@pytest.mark.parametrize("sent_for, stopped", [(None, False), (120, False), (400, True)])
def test_views_stop_a_machine_whose_own_stop_call_vast_did_not_take(sent_for, stopped, fake_vast, machines):
    iid = worker(fake_vast)
    machines.replies[("job1", "status")] = status("done", stopping="done", stop_sent_for=sent_for)
    workers.views()
    assert (fake_vast.instances[iid]["intended_status"] == "stopped") is stopped  # (the fake checks the key)
    assert len(fake_vast.calls("PUT", "/")) == stopped


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
    rec = core.load("job1")
    assert rec["last_seen"]["stop_reason"] == "user" and rec["stopped_since"] == pytest.approx(
        time.time(), abs=60
    )


@pytest.mark.parametrize("committed", [True, False])
def test_stop_keeps_the_stop_note_its_final_save_committed(committed, fake_vast, machines):
    worker(fake_vast)
    left = note(outputs={"files": 1, "bytes": 2.0e9, "sample": ["best.pt"]})
    machines.replies[("job1", "save")] = saved(note=left, note_committed=committed, unpushed=0)
    workers.stop("job1", out())
    assert "--final" in machines.calls[0][1]  # (the save that writes the note)
    kept = {**left, "unpushed": 0, "recorded_at": pytest.approx(time.time(), abs=60)}
    assert core.load("job1")["stop_note"] == (kept if committed else None)


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


@pytest.mark.parametrize("state", ["running", "exited"])
def test_a_launch_cut_off_after_its_upload_may_hold_work(state, fake_vast, fake_github, machines):
    """Delete checks it (a stopped one that left no stop note cannot be checked: --force), and stop stops it."""
    iid = worker(fake_vast, status=state, phase="setup", uploaded=True)
    machines.replies[("job1", "check")] = {**CLEAN, "unpushed": 1}
    machines.replies[("job1", "save")] = {"committed": 1, "pushed": True, "error": None}
    fails(3, workers.delete, "job1", False, out())
    assert iid in fake_vast.instances and core.record_path("job1").exists()
    workers.stop("job1", out())
    assert not vast.wants_running(fake_vast.instances[iid])


# ---- delete --------------------------------------------------------------------------------------


def test_deleting_a_pending_worker_only_forgets_it(fake_vast):
    core.save(record(phase="pending", pending={"reason": "it would cost $6.00/hr", "max_dph": 6.0}))
    workers.delete("job1", False, out())
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
    assert not github.key_file(repo["deploy_key"]).exists()
    assert (fake_github.branch_sha("ada/proj", "agent/job1") is not None) is worked


def test_a_shared_deploy_key_goes_with_the_last_worker_holding_it(fake_vast, fake_github, machines, repo):
    git(fake_github.bare("ada/proj"), "branch", "agent/job2", repo["start_sha"])
    worker(fake_vast, repo=repo)
    worker(fake_vast, "job2", repo={**repo, "branch": "agent/job2"})
    machines.replies[("job1", "check")] = machines.replies[("job2", "check")] = CLEAN
    workers.delete("job1", False, out())
    assert list(fake_github.keys["ada/proj"]) == [101] and github.key_file(repo["deploy_key"]).exists()
    workers.delete("job2", False, out())
    assert fake_github.keys["ada/proj"] == {} and not github.key_file(repo["deploy_key"]).exists()


def test_delete_destroys_the_machine_of_an_unconfirmed_rent_found_by_its_label(fake_vast):
    iid = fake_vast.add_instance(1, "cs:job1:abcd1234", status="loading")
    fake_vast.add_instance(1, "cs:job1:00000000", status="loading")  # (not this rent's label: left alone)
    core.save(record(phase="renting", rent={"label": "cs:job1:abcd1234", "offer_id": 1, "at": 0}))
    workers.delete("job1", False, out())
    assert iid not in fake_vast.instances and len(fake_vast.instances) == 1
    assert not core.record_path("job1").exists()


# ---- delete a stopped worker: from its stop note, without starting it ----------------------------


def test_a_stopped_worker_whose_stop_note_shows_nothing_left_is_deleted_without_starting_it(
    fake_vast, fake_github, machines, repo, tmp_path
):
    iid = worker(fake_vast, status="exited", repo=repo)
    push_to_branch(fake_github, tmp_path, NOTE, json.dumps(note()))
    workers.delete("job1", False, out())
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
    iid = worker(fake_vast, status="exited", repo=repo)
    push_to_branch(fake_github, tmp_path, NOTE, json.dumps(note(**left)))
    message = fails(3, workers.delete, "job1", False, out()).message
    assert named in message and "crab-student start job1" in message  # (start it to copy the work)
    assert iid in fake_vast.instances and core.record_path("job1").exists() and not machines.calls


def test_a_stop_note_from_before_the_last_start_is_not_trusted(
    fake_vast, fake_github, machines, repo, tmp_path
):
    """(A stop that could not push its note leaves the previous stop's on the branch.)"""
    started = time.time() - 3600
    iid = worker(fake_vast, status="exited", repo=repo, last_start=started)
    push_to_branch(fake_github, tmp_path, NOTE, json.dumps(note(stopped_at=int(started) - 600)))
    fails(3, workers.delete, "job1", False, out())
    assert iid in fake_vast.instances and core.record_path("job1").exists() and not machines.calls


@pytest.mark.parametrize("text", [None, "not json", "[]", '{"stopped_at": "a while ago"}'])
def test_a_stopped_worker_without_a_stop_note_to_trust_needs_a_start_or_force(
    text, fake_vast, fake_github, machines, repo, tmp_path
):
    iid = worker(fake_vast, status="exited", repo=repo)
    if text is not None:
        push_to_branch(fake_github, tmp_path, NOTE, text)
    assert "crab-student start job1" in fails(3, workers.delete, "job1", False, out()).message
    assert iid in fake_vast.instances and core.record_path("job1").exists()
    workers.delete("job1", True, out())
    assert not fake_vast.instances and not core.record_path("job1").exists() and not machines.calls


@pytest.mark.parametrize(
    "save, deleted",
    [({}, True), ({"pushed": False, "unpushed": 1}, False), ({"note_committed": False}, False)],
)
def test_a_worker_stopped_with_stop_is_deleted_without_starting_it_when_its_save_left_nothing(
    save, deleted, fake_vast, fake_github, machines
):
    iid = worker(fake_vast)
    machines.replies[("job1", "save")] = saved(**save)
    workers.stop("job1", out())
    if deleted:
        workers.delete("job1", False, out())
    else:
        fails(3, workers.delete, "job1", False, out())
    assert (iid in fake_vast.instances) is not deleted and core.record_path("job1").exists() is not deleted
    assert [call[1][0] for call in machines.calls] == ["save"]  # (never started again to check it)
    assert len(fake_vast.calls("PUT", "/")) == 1  # (the stop)


def test_the_records_stop_note_counts_only_until_the_next_start(
    fake_vast, fake_github, machines, repo, tmp_path
):
    """Started again after `stop`, the worker stopped by itself: the note it left on the branch counts."""
    started = time.time() - 3600
    old = {**note(stopped_at=int(started) - 7200), "unpushed": 0, "recorded_at": started - 7200}
    iid = worker(fake_vast, status="exited", repo=repo, last_start=started, stop_note=old)
    left = note(outputs={"files": 1, "bytes": 1.0e9, "sample": ["best.pt"]})
    push_to_branch(fake_github, tmp_path, NOTE, json.dumps(left))
    assert "best.pt" in fails(3, workers.delete, "job1", False, out()).message
    assert iid in fake_vast.instances and core.record_path("job1").exists()


def test_the_newer_of_the_two_stop_notes_counts(fake_vast, fake_github, machines, repo, tmp_path):
    """Stopped with `stop`, started from Vast's website unseen, and stopped by itself: its own note is newer."""
    started = time.time() - 3600
    kept = {**note(stopped_at=int(started) + 600), "unpushed": 0, "recorded_at": started + 600}
    iid = worker(fake_vast, status="exited", repo=repo, last_start=started, stop_note=kept)
    left = note(outputs={"files": 1, "bytes": 1.0e9, "sample": ["best.pt"]})  # (a minute ago)
    push_to_branch(fake_github, tmp_path, NOTE, json.dumps(left))
    assert "best.pt" in fails(3, workers.delete, "job1", False, out()).message
    assert iid in fake_vast.instances


def test_a_start_seen_from_here_makes_the_old_stop_notes_stale(fake_vast, fake_github, machines, repo):
    """Started from Vast's website: once list sees it running, notes from before no longer count."""
    kept = {**note(), "unpushed": 0, "recorded_at": time.time() - 60}
    iid = worker(fake_vast, status="exited", repo=repo, stop_note=kept)
    workers.views()  # (seen stopped)
    fake_vast.instances[iid].update(actual_status="running", intended_status="running", cur_state="running")
    workers.views()  # (seen running again: it may be doing new work)
    fake_vast.instances[iid].update(actual_status="exited", intended_status="stopped", cur_state="stopped")
    rec = core.load("job1")
    assert rec["stop_note"] is None and rec["last_start"] == pytest.approx(time.time(), abs=60)
    fails(3, workers.delete, "job1", False, out())
    assert iid in fake_vast.instances


def test_a_stop_note_counts_only_when_github_can_be_asked_about_a_newer_one(fake_vast, machines, monkeypatch):
    kept = {**note(), "unpushed": 0, "recorded_at": time.time() - 60}
    iid = worker(fake_vast, status="exited", stop_note=kept)

    def unreachable(*a):
        raise core.CrabError("GitHub: could not connect")

    monkeypatch.setattr(github, "read_file", unreachable)
    fails(3, workers.delete, "job1", False, out())
    assert iid in fake_vast.instances


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
