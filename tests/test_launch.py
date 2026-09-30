"""Launching through the fakes: the happy path and the repo, the approval gate, the rent loop's failure modes,
interrupted launches, and start and extend."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from fake_gh import git
from fake_vast import cpu_offer, offer

from crab_student import cli, core, launch, machine, offers, vast

TOKEN = "sk-ant-oat01-" + "x" * 80  # (the Claude token the ready fixture stores)
HF = "hf_the-task-secret-0042"
RESULT_KEYS = {"name", "state", "result_line", "warnings", "needs_approval", "instance_id", "machine", "dph",
               "hours", "est_usd", "budget_usd", "limit_hours", "repo", "branch"}  # fmt: skip


@pytest.fixture(autouse=True)
def world(fake_vast, fake_github, user_repo, machines, ready, monkeypatch):
    """Two offers on different machines (a 1h task gets a budget of 2 x $0.30 = $0.60), from the user's repo."""
    fake_vast.offers = [offer(1, dph_total=0.30), offer(2, dph_total=0.35)]
    monkeypatch.chdir(user_repo)


def args(name: str = "job1", req: offers.Req | None = None, **kw) -> launch.LaunchArgs:
    fields = {"budget": None, "brief": "# Task\nDo the thing.\n", "repo_path": None, "no_repo": False,
              "secrets": [], "model": None, "fake_claude": False, "dry_run": False}  # fmt: skip
    return launch.LaunchArgs(name=name, req=req or offers.Req(hours=1), **{**fields, **kw})


def out() -> launch.Progress:
    return launch.Progress(io.StringIO())


def go(a: launch.LaunchArgs) -> dict:
    return launch.launch(a, out())


def fails(code: int, fn, *a) -> core.CrabError:
    with pytest.raises(core.CrabError) as e:
        fn(*a)
    assert e.value.code == code, e.value.message
    return e.value


def rents(fake_vast) -> list:
    return fake_vast.calls("PUT", "/api/v0/asks/")


def starts(fake_vast) -> list:
    return [r for r in fake_vast.calls("PUT", "/api/v0/instances/") if r[2] == {"state": "running"}]


def gh_writes(fake_github) -> list:
    return [c for c in fake_github.calls if c[:2] == ("api", "-X") and c[2] != "GET"]


def machine_runs(fake_vast, name: str = "job1") -> bool:
    """The worker's machine runs."""
    return vast.running(fake_vast.instances.get(core.load(name)["instance_id"]))


def stop_machine(fake_vast, name: str = "job1") -> None:
    fake_vast.instances[core.load(name)["instance_id"]].update(
        actual_status="exited", intended_status="stopped", cur_state="stopped"
    )


def other_worker(fake_vast, status: str = "running") -> int:
    """Another ready worker (only what the machine count reads): its instance id."""
    down = {} if status == "running" else {"intended_status": "stopped", "cur_state": "stopped"}
    iid = fake_vast.add_instance(9, "cs:busy:0", status=status, **down)
    core.save({"name": "busy", "phase": "ready", "instance_id": iid})
    return iid


def record_spawns(monkeypatch) -> list[str]:
    """Every command line (and environment) handed to a subprocess from now on, as text."""
    seen: list[str] = []

    class Recording(subprocess.Popen):
        def __init__(self, cmd, *rest, **kw):
            seen.append(f"{cmd!r} {kw.get('env')!r}")
            super().__init__(cmd, *rest, **kw)

    monkeypatch.setattr(subprocess, "Popen", Recording)
    return seen


def user_state(repo: Path) -> list[str]:
    """What the user has: HEAD, branches, stash, index and working tree, as git shows them."""
    return [git(repo, *a) for a in (["rev-parse", "HEAD"], ["for-each-ref"], ["stash", "list"],
                                    ["diff", "--cached"], ["diff"], ["status", "--porcelain"])]  # fmt: skip


# ---- the happy path and the repo -----------------------------------------------------------------


def test_a_launch_rents_once_and_sets_up_the_worker(fake_vast, fake_github, user_repo, machines, monkeypatch,
                                                    capsys):  # fmt: skip
    labels = []  # the record's rent label at the moment Vast gets the rent request
    real_rent = fake_vast._rent

    def rent(offer_id, body):
        labels.append(core.load("job1")["rent"]["label"])
        return real_rent(offer_id, body)

    monkeypatch.setattr(fake_vast, "_rent", rent)
    monkeypatch.setattr("sys.stdin", io.StringIO("# Task\nDo the thing.\n"))
    code = cli.main(["launch", "job1", "--hours", "1", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    res, rec = json.loads(captured.out), core.load("job1")
    assert res["state"] == "working" and res["branch"] == "agent/job1" and res.keys() >= RESULT_KEYS
    assert rec["phase"] == "ready" and res["instance_id"] == rec["instance_id"]
    assert list(fake_vast.instances) == [rec["instance_id"]]
    (sent,) = rents(fake_vast)
    assert sent[1] == "/api/v0/asks/1/" and labels == [sent[2]["label"]] == [rec["rent"]["label"]]
    assert (rec["budget_usd"], rec["limit_hours"]) == (0.6, 2.0)
    head = git(user_repo, "rev-parse", "HEAD").strip()
    assert fake_github.branch_sha("ada/proj", "agent/job1") == rec["repo"]["start_sha"] == head
    ((key_id, key),) = fake_github.keys["ada/proj"].items()
    public = Path(f"{launch.key_file('job1')}.pub").read_text().strip()
    assert key_id == rec["repo"]["deploy_key_id"] and key["key"] == public and key["read_only"] == "false"
    config = json.loads(machines.uploads["job1"]["config.json"][0])
    assert (config["branch"], config["limit_hours"]) == ("agent/job1", 2.0)
    assert config["session_id"] == rec["session_id"]
    assert fake_vast.ssh_keys[rec["instance_id"]] == ["ssh-ed25519 AAAA test"]
    assert f"Host {machine.alias('job1')}" in machine.config_file().read_text()


def test_secrets_reach_the_machine_only_in_private_files(fake_vast, fake_github, machines, monkeypatch):
    core.set_secret(core.secret_key("HF_TOKEN"), HF)
    core.set_secret_names(["HF_TOKEN"])
    spawned, log = record_spawns(monkeypatch), io.StringIO()
    launch.launch(args(secrets=["HF_TOKEN"]), launch.Progress(log))
    files = machines.uploads["job1"]
    holders = {name for name, (data, _) in files.items() if TOKEN.encode() in data or HF.encode() in data}
    assert holders == {"claude.env", "secrets.env"}
    assert all(files[name][1] == 0o600 for name in ("claude.env", "secrets.env", "deploy_key", "config.json"))
    assert json.loads(files["config.json"][0])["secret_names"] == ["HF_TOKEN"]
    said = log.getvalue() + str(fake_vast.requests) + str(fake_github.calls) + str(machines.calls)
    local = [p.read_bytes() for p in Path.home().rglob("*") if p.is_file()]  # records, brief, ssh config...
    assert spawned
    for secret in (TOKEN, HF):
        assert secret not in said and not any(secret in cmd for cmd in spawned)
        assert not any(secret.encode() in data for data in local)


def test_uncommitted_changes_are_carried_and_untracked_files_reported(fake_github, user_repo):
    (user_repo / "README.md").write_text("changed\n")
    (user_repo / "notes.txt").write_text("mine\n")
    before = user_state(user_repo)
    res = go(args())
    rec, bare = core.load("job1"), fake_github.bare("ada/proj")
    assert git(bare, "rev-parse", "agent/job1").strip() == rec["repo"]["start_sha"]
    assert git(bare, "rev-parse", "agent/job1^").strip() == git(user_repo, "rev-parse", "HEAD").strip()
    assert "changed" in git(bare, "show", "agent/job1:README.md")
    assert "notes.txt" not in git(bare, "ls-tree", "--name-only", "agent/job1")
    assert (rec["repo"]["carried"], rec["repo"]["untracked"]) == (["README.md"], ["notes.txt"])
    assert len(res["warnings"]) == 1 and "notes.txt" in res["warnings"][0]
    assert user_state(user_repo) == before


def test_no_repo_works_in_a_private_workspace_repo(fake_github, machines, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # (no repo needed)
    go(args(no_repo=True))
    rec, slug = core.load("job1"), "ada/crab-student-work"
    main = git(fake_github.bare(slug), "rev-parse", "main").strip()
    assert rec["phase"] == "ready" and list(fake_github.keys[slug]) == [rec["repo"]["deploy_key_id"]]
    assert fake_github.branch_sha(slug, "agent/job1") == rec["repo"]["start_sha"] == main
    assert any(c[3] == "user/repos" and "private=true" in c for c in gh_writes(fake_github))
    config = json.loads(machines.uploads["job1"]["config.json"][0])
    assert config["repo_dir"] == "/root/work/crab-student-work"


# ---- the approval gate ---------------------------------------------------------------------------


def test_a_budget_over_auto_approve_waits_and_approve_rents_within_it(fake_vast, fake_github):
    core.set_config("auto_approve_usd", "0.5")
    res, rec = go(args()), core.load("job1")
    assert res["exit_code"] == 3 and res["needs_approval"] and res.keys() >= RESULT_KEYS
    assert rec["phase"] == "pending" and rec["pending"]["action"] == "launch"
    assert not fake_vast.calls("PUT", "/") and not gh_writes(fake_github)
    assert fake_github.branch_sha("ada/proj", "agent/job1") is None
    # approve plans again: offer 4 would be a backup, but $0.62 for the 1h estimate is over the approved $0.60
    fake_vast.offers = [offer(3, dph_total=0.50), offer(4, dph_total=0.62)]
    res, rec = launch.approve("job1", out()), core.load("job1")
    assert res["exit_code"] == 0 and rec["phase"] == "ready" and rec["pending"] is None
    assert rec["budget_usd"] == 0.6 and [o["id"] for o in rec["offers"]] == [3]
    assert [r[1] for r in rents(fake_vast)] == ["/api/v0/asks/3/"]


def test_decline_forgets_a_pending_launch(fake_vast):
    core.set_config("auto_approve_usd", "0.5")
    go(args())
    launch.decline("job1")
    assert not core.record_path("job1").exists() and not launch.brief_file("job1").exists()
    assert not fake_vast.calls("PUT", "/")


@pytest.mark.parametrize("status", ["running", "exited"])
def test_max_running_machines_holds_a_launch_while_another_machine_runs(status, fake_vast):
    core.set_config("max_running_machines", "1")
    other_worker(fake_vast, status)
    res, held = go(args()), status == "running"
    assert res["exit_code"] == (3 if held else 0) and (core.load("job1")["phase"] == "pending") is held
    assert len(rents(fake_vast)) == (0 if held else 1)


# ---- the rent loop -------------------------------------------------------------------------------


def test_a_taken_offer_moves_on_to_the_next(fake_vast):
    fake_vast.rent_outcome[1] = "taken"
    go(args())
    rec = core.load("job1")
    assert rec["phase"] == "ready" and fake_vast.instances[rec["instance_id"]]["offer_id"] == 2
    assert [f["id"] for f in rec["failed_offers"]] == [1] and len(rents(fake_vast)) == 2
    # the same budget; the time limit follows the backup's price
    assert (rec["budget_usd"], rec["limit_hours"]) == (0.6, 1.71)


@pytest.mark.parametrize("boot", ["boot_error", "exits", "never"])
def test_a_machine_that_does_not_boot_is_destroyed_and_a_backup_rented(boot, fake_vast, monkeypatch):
    monkeypatch.setattr(launch, "BOOT_TIMEOUT", 0.3)
    fake_vast.boot[1] = boot
    go(args())
    rec = core.load("job1")
    assert rec["phase"] == "ready" and fake_vast.instances[rec["instance_id"]]["offer_id"] == 2
    assert len(fake_vast.calls("DELETE", "/")) == 1 and list(fake_vast.instances) == [rec["instance_id"]]
    assert [f["id"] for f in rec["failed_offers"]] == [1]


def test_a_running_machine_with_an_old_error_in_its_status_is_kept(fake_vast, monkeypatch):
    fake_vast.boot[1] = "stale_error"
    answers = iter([False, False])  # (ssh answers from the third poll)
    monkeypatch.setattr(machine, "reachable", lambda name: next(answers, True))
    go(args())
    rec = core.load("job1")
    assert rec["phase"] == "ready" and fake_vast.instances[rec["instance_id"]]["offer_id"] == 1
    assert len(rents(fake_vast)) == 1 and not fake_vast.calls("DELETE", "/")


def test_when_every_offer_fails_the_launch_fails_with_exit_4(fake_vast):
    fake_vast.rent_outcome[1] = "taken"
    fake_vast.boot[2] = "exits"
    fails(4, go, args())
    rec = core.load("job1")
    assert rec["phase"] == "failed" and rec["error"] and rec["instance_id"] is None
    assert not fake_vast.instances  # (the one that did not boot was destroyed)


@pytest.mark.parametrize("outcome", ["error_created", "slow_created"])
def test_a_rent_vast_did_not_confirm_is_found_by_its_label(outcome, fake_vast, monkeypatch):
    monkeypatch.setattr(vast, "RENT_TIMEOUT", 0.5)
    monkeypatch.setattr(launch, "LABEL_WAIT", 5.0)  # (the search stops as soon as the label shows)
    fake_vast.slow_seconds = 1.5
    fake_vast.rent_outcome[1] = outcome
    go(args())
    rec = core.load("job1")
    assert rec["phase"] == "ready" and len(rents(fake_vast)) == 1
    assert list(fake_vast.instances) == [rec["instance_id"]]
    assert fake_vast.instances[rec["instance_id"]]["label"] == rec["rent"]["label"]


def test_an_unconfirmed_rent_that_rented_nothing_stops_and_start_looks_again(fake_vast):
    fake_vast.rent_outcome[1] = "error_none"
    fails(1, go, args())
    rec = core.load("job1")
    (sent,) = rents(fake_vast)
    assert sent[1] == "/api/v0/asks/1/"  # (no other offer was tried)
    assert rec["phase"] == "renting" and rec["rent"]["label"] == sent[2]["label"]
    fails(1, launch.start, "job1", out())  # still no machine with that label: nothing is rented again
    iid = fake_vast.add_instance(1, rec["rent"]["label"], status="loading")  # Vast shows it late
    launch.start("job1", out())
    rec = core.load("job1")
    assert rec["phase"] == "ready" and rec["instance_id"] == iid and len(rents(fake_vast)) == 1


def test_no_credit_fails_the_launch_without_trying_other_offers(fake_vast):
    fake_vast.rent_outcome[1] = "no_credit"
    assert "credit" in fails(1, go, args()).message
    assert core.load("job1")["phase"] == "failed" and len(rents(fake_vast)) == 1 and not fake_vast.instances


# ---- interruptions and setup failures ------------------------------------------------------------


@pytest.mark.parametrize("where", ["boot", "upload"])
def test_an_interrupted_launch_is_continued_by_start(where, fake_vast, machines, monkeypatch):
    if where == "boot":
        real_wait = launch.wait_boot

        def interrupted(*a):
            monkeypatch.setattr(launch, "wait_boot", real_wait)
            raise KeyboardInterrupt

        monkeypatch.setattr(launch, "wait_boot", interrupted)
        with pytest.raises(KeyboardInterrupt):
            go(args())
    else:
        machines.setup_result = machine.Unreachable("could not reach job1 over ssh for the upload")
        fails(1, go, args())
        machines.setup_result = None
    rec = core.load("job1")
    assert rec["phase"] == ("booting" if where == "boot" else "setup")
    assert launch.start("job1", out())["state"] == "working" and core.load("job1")["phase"] == "ready"
    assert len(rents(fake_vast)) == 1 and list(fake_vast.instances) == [rec["instance_id"]]
    assert "job1" in machines.uploads


def test_start_restarts_the_stopped_machine_of_a_launch_cut_off_during_setup(fake_vast, machines):
    """The worker may be at work there: its machine is started again and kept, and setup runs again."""
    machines.setup_result = machine.Unreachable("the upload and setup on job1 did not finish")
    fails(1, go, args())
    machines.setup_result, machines.uploads = None, {}
    stop_machine(fake_vast)  # (the dead-man switch or the watchdog stopped it meanwhile)
    fake_vast.start_polls = 3  # (it reads "exited", with an error, until its host has room)
    iid = core.load("job1")["instance_id"]
    assert launch.start("job1", out())["state"] == "working"
    assert core.load("job1")["phase"] == "ready" and len(starts(fake_vast)) == 1 and machine_runs(fake_vast)
    assert list(fake_vast.instances) == [iid] and "job1" in machines.uploads


def test_a_failed_setup_destroys_the_machine_and_start_retries_with_fresh_offers(fake_vast, machines):
    machines.setup_result = core.CrabError("setup failed at step 4 (Claude Code): the token was refused", 5)
    fails(5, go, args())
    rec = core.load("job1")
    assert rec["phase"] == "failed" and rec["instance_id"] is None and not fake_vast.instances
    machines.setup_result = None
    fake_vast.offers = [offer(3, dph_total=0.32)]
    assert launch.start("job1", out())["state"] == "working"
    rec = core.load("job1")
    assert rec["phase"] == "ready" and fake_vast.instances[rec["instance_id"]]["offer_id"] == 3


# ---- what a launch refuses, dry runs, the rent body ----------------------------------------------


def test_dry_run_rents_nothing_and_writes_nothing(fake_vast, fake_github, user_repo):
    core.set_config("auto_approve_usd", "0.5")
    (user_repo / "README.md").write_text("changed\n")
    res = go(args(dry_run=True))
    assert res["state"] == "dry run" and res["exit_code"] == 0 and res.keys() >= RESULT_KEYS
    assert res["needs_approval"]  # (said, while a dry run still exits 0)
    assert all(method == "GET" or path == "/api/v0/bundles/" for method, path, _ in fake_vast.requests)
    assert not gh_writes(fake_github) and not core.record_path("job1").exists()


@pytest.mark.parametrize(
    "case,code", [("bad name", 2), ("taken name", 2), ("branch exists", 2), ("not admin", 1)]
)
def test_refused_launches_rent_nothing(case, code, fake_vast, fake_github):
    if case == "taken name":
        core.save({"name": "job1", "phase": "pending"})
    elif case == "branch exists":
        git(fake_github.bare("ada/proj"), "branch", "agent/job1", "main")
    elif case == "not admin":
        fake_github.admin["ada/proj"] = False
    fails(code, go, args(name="Job_1" if case == "bad name" else "job1"))
    assert not fake_vast.calls("PUT", "/") and not gh_writes(fake_github)


def test_a_name_another_launch_takes_while_this_one_plans_rents_nothing(fake_vast, fake_github, monkeypatch):
    real_plan, other = offers.plan, {"name": "job1", "phase": "pending"}

    def plan(*a):
        core.save(other)  # (the other launch passes the gate first)
        return real_plan(*a)

    monkeypatch.setattr(offers, "plan", plan)
    fails(2, go, args())
    assert core.load("job1") == other and not fake_vast.calls("PUT", "/") and not gh_writes(fake_github)


@pytest.mark.parametrize("gpus,host,disabled", [(0, offer, True), (1, offer, False), (0, cpu_offer, False)])
def test_nvidia_disable_require_only_for_a_cpu_task_on_a_gpu_host(gpus, host, disabled, fake_vast):
    fake_vast.offers = [host(1)]
    go(args(req=offers.Req(hours=1, gpus=gpus)))
    (sent,) = rents(fake_vast)
    assert sent[2]["env"].get("NVIDIA_DISABLE_REQUIRE") == ("1" if disabled else None)


# ---- extend and start ----------------------------------------------------------------------------


def test_extend_pushes_the_new_limit_to_a_running_machine(machines):
    go(args())
    res, rec = launch.extend("job1", 2), core.load("job1")
    assert res["exit_code"] == 0
    assert (rec["budget_usd"], rec["limit_hours"]) == (1.2, 4.0)
    assert machines.calls == [("job1", ["set-limit", "4.0"], None)]


def test_extend_of_a_stopped_worker_is_pushed_by_the_next_start(fake_vast, machines):
    go(args())
    stop_machine(fake_vast)
    launch.extend("job1", 2)
    assert not machines.calls
    res = launch.start("job1", out())
    assert res["state"] == "working" and res.keys() >= RESULT_KEYS and len(starts(fake_vast)) == 1
    assert machines.calls == [("job1", ["set-limit", "4.0"], None)]


def test_extend_over_auto_approve_waits_for_approval(machines):
    go(args())
    core.set_config("auto_approve_usd", "1")
    res, rec = launch.extend("job1", 10), core.load("job1")  # (the budget would be $0.60 + 10 x $0.30)
    assert res["exit_code"] == 3 and rec["pending"]["action"] == "extend" and rec["limit_hours"] == 2.0
    assert not machines.calls
    launch.approve("job1", out())
    rec = core.load("job1")
    assert rec["pending"] is None and (rec["budget_usd"], rec["limit_hours"]) == (3.6, 12.0)


def test_start_refuses_once_the_time_limit_is_used_up(fake_vast):
    go(args())
    stop_machine(fake_vast)
    rec = core.load("job1")
    rec["last_seen"] = {"used_hours": 2.0}
    core.save(rec)
    fails(3, launch.start, "job1", out())
    assert not starts(fake_vast)


def test_start_waits_while_the_machine_reads_exited_and_forgets_why_it_stopped(fake_vast, machines):
    go(args())
    stop_machine(fake_vast)
    rec = core.load("job1")
    rec["last_seen"] = {"used_hours": 0.5, "stop_reason": "user"}
    core.save(rec)
    fake_vast.start_polls = 3  # (it reads "exited", with an error, until its host has room)
    assert launch.start("job1", out())["state"] == "working" and machine_runs(fake_vast)
    assert core.load("job1")["last_seen"]["stop_reason"] is None and not fake_vast.calls("DELETE", "/")


@pytest.mark.parametrize("then", ["approve", "start again"])
@pytest.mark.parametrize("was", ["stopped", "failed"])
def test_start_waits_for_approval_while_too_many_machines_run(was, then, fake_vast, machines):
    if was == "stopped":
        go(args())
        stop_machine(fake_vast)
    else:
        machines.setup_result = core.CrabError("setup failed at step 2 (repository): clone failed", 5)
        fails(5, go, args())
        machines.setup_result = None
    core.set_config("max_running_machines", "1")
    busy = other_worker(fake_vast)
    res = launch.start("job1", out())
    assert res["exit_code"] == 3 and core.load("job1")["pending"]["action"] == "start"
    assert not machine_runs(fake_vast)
    if then == "approve":
        launch.approve("job1", out())
    else:  # (the other machine stopped meanwhile)
        fake_vast.instances[busy].update(actual_status="exited", intended_status="stopped")
        launch.start("job1", out())
    rec = core.load("job1")
    assert rec["phase"] == "ready" and rec["pending"] is None and machine_runs(fake_vast)


def test_start_stops_a_machine_that_reports_its_limit_reached(fake_vast, machines):
    go(args())
    stop_machine(fake_vast)
    machines.replies[("job1", "set-limit")] = {"limit_hours": 2.0, "used_hours": 2.0}
    fails(3, launch.start, "job1", out())
    iid = core.load("job1")["instance_id"]
    assert len(starts(fake_vast)) == 1 and fake_vast.instances[iid]["intended_status"] == "stopped"
    fails(3, launch.start, "job1", out())  # (it knows now: no second boot)
    assert len(starts(fake_vast)) == 1
