"""Launching through the fakes: the happy path and the repo, the mode and Claude settings, the approval gate, the
rent loop's failure modes, interrupted launches, and start."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from fake_gh import git
from fake_vast import cpu_offer, offer

from crab_student import cli, core, github, launch, machine, offers, vast

TOKEN = "sk-ant-oat01-" + "x" * 80  # (the Claude token the ready fixture stores)
HF = "hf_the-task-secret-0042"
RESULT_KEYS = {"name", "state", "result_line", "warnings", "needs_approval", "instance_id", "machine", "dph",
               "mode", "repo", "branch"}  # fmt: skip


@pytest.fixture(autouse=True)
def world(fake_vast, fake_github, user_repo, machines, ready, monkeypatch):
    """Two offers on different machines, from the user's repo."""
    fake_vast.offers = [offer(1, dph_total=0.30), offer(2, dph_total=0.35)]
    monkeypatch.chdir(user_repo)


def args(name: str = "job1", req: offers.Req | None = None, **kw) -> launch.LaunchArgs:
    fields = {"brief": "# Task\nDo the thing.\n", "repo_path": None, "no_repo": False, "secrets": [],
              "fake_claude": False, "dry_run": False}  # fmt: skip
    return launch.LaunchArgs(name=name, req=req or offers.Req(), **{**fields, **kw})


PER_MODEL = {"claude-opus-5-5": {"effortLevel": "high"}}  # (where Claude Code keeps the user's /effort)


def own_claude_settings(**settings) -> None:
    """The user's own ~/.claude/settings.json."""
    path = Path.home() / ".claude" / "settings.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"theme": "dark", **settings}))


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
    """The agent's machine runs."""
    return vast.running(fake_vast.instances.get(core.load(name)["instance_id"]))


def stop_machine(fake_vast, name: str = "job1") -> None:
    fake_vast.instances[core.load(name)["instance_id"]].update(
        actual_status="exited", intended_status="stopped", cur_state="stopped"
    )


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


def test_a_launch_rents_once_and_sets_up_the_agent(fake_vast, fake_github, user_repo, machines, monkeypatch,
                                                    capsys):  # fmt: skip
    labels = []  # the record's rent label at the moment Vast gets the rent request
    real_rent = fake_vast._rent

    def rent(offer_id, body):
        labels.append(core.load("job1")["rent"]["label"])
        return real_rent(offer_id, body)

    monkeypatch.setattr(fake_vast, "_rent", rent)
    monkeypatch.setattr("sys.stdin", io.StringIO("# Task\nDo the thing.\n"))
    code = cli.main(["launch", "job1", "--json"])  # (no hours: they are optional)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    res, rec = json.loads(captured.out), core.load("job1")
    assert res["state"] == "working" and res["branch"] == "agent/job1" and res.keys() >= RESULT_KEYS
    assert rec["phase"] == "ready" and res["instance_id"] == rec["instance_id"]
    assert list(fake_vast.instances) == [rec["instance_id"]]
    (sent,) = rents(fake_vast)
    assert sent[1] == "/api/v0/asks/1/" and labels == [sent[2]["label"]] == [rec["rent"]["label"]]
    assert rec["last_start"] and rec["req"]["hours"] is None and "limit_hours" not in rec
    head = git(user_repo, "rev-parse", "HEAD").strip()
    assert fake_github.branch_sha("ada/proj", "agent/job1") == rec["repo"]["start_sha"] == head
    ((key_id, key),) = fake_github.keys["ada/proj"].items()
    public = Path(f"{github.key_file(rec['repo']['deploy_key'])}.pub").read_text().strip()
    assert key_id == rec["repo"]["deploy_key"]["id"] and key["key"] == public and key["read_only"] == "false"
    config = json.loads(machines.uploads["job1"]["config.json"][0])
    assert (config["branch"], config["mode"], config["claude_settings"]) == ("agent/job1", "colleague", {})
    assert config["session_id"] == rec["session_id"] and not {"limit_hours", "model"} & config.keys()
    assert fake_vast.ssh_keys[rec["instance_id"]] == ["ssh-ed25519 AAAA test"]
    assert f"Host {machine.alias('job1')}" in machine.config_file().read_text()


# ---- the mode and Claude settings ----------------------------------------------------------------


def test_the_mode_and_claude_settings_reach_the_machine(machines):
    own_claude_settings(model="opus", effortLevel="medium", fastMode=True)
    core.set_config("effort", "low")
    core.set_config("mode", "autonomous")
    res = go(args(model="sonnet"))
    rec, config = core.load("job1"), json.loads(machines.uploads["job1"]["config.json"][0])
    expected = {
        "model": "sonnet",
        "effortLevel": "low",
        "fastMode": True,
    }  # (the flag, the setting, the user's)
    assert rec["claude_settings"] == config["claude_settings"] == expected
    assert rec["mode"] == config["mode"] == res["mode"] == "autonomous"


@pytest.mark.parametrize(
    "flags, settings, own, expected",
    [
        ({}, {}, None, {}),
        ({}, {}, {"model": "opus", "effortLevel": "high", "fastMode": False},
         {"model": "opus", "effortLevel": "high", "fastMode": False}),
        ({}, {"model": "haiku", "effort": "xhigh", "fast_mode": "on"}, {"model": "opus", "fastMode": False},
         {"model": "haiku", "effortLevel": "xhigh", "fastMode": True}),
        ({"model": "sonnet", "effort": "low", "fast": False}, {"model": "haiku", "fast_mode": "on"},
         {"effortLevel": "high", "fastMode": True}, {"model": "sonnet", "effortLevel": "low", "fastMode": False}),
        ({}, {}, {"model": "opus", "modelSettings": PER_MODEL}, {"model": "opus", "modelSettings": PER_MODEL}),
        ({"effort": "low"}, {}, {"modelSettings": PER_MODEL}, {"effortLevel": "low"}),  # (a chosen effort wins)
        ({}, {"effort": "medium"}, {"modelSettings": PER_MODEL}, {"effortLevel": "medium"}),
    ],
)  # fmt: skip
def test_each_claude_setting_comes_from_the_flag_else_the_setting_else_the_users_own(
    flags, settings, own, expected
):
    for key, value in settings.items():
        core.set_config(key, value)
    if own is not None:
        own_claude_settings(**own)
    assert launch.claude_settings(args(**flags), core.load_config()) == expected


def test_an_unreadable_claude_settings_file_is_copied_from_as_nothing():
    path = Path.home() / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text('{"model": "opus",')
    assert launch.claude_settings(args(), core.load_config()) == {}


def test_an_interactive_agent_is_opened_in_vs_code(machines):
    res = go(args(mode="interactive"))
    assert json.loads(machines.uploads["job1"]["config.json"][0])["mode"] == "interactive"
    assert res["mode"] == "interactive" and "crab-student open job1 --vscode" in res["result_line"]
    other = go(args("job2"))
    assert "crab-student open job2" in other["result_line"] and "--vscode" not in other["result_line"]


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


def test_agents_started_within_a_day_share_one_deploy_key(fake_github, machines):
    go(args("job1"))
    go(args("job2"))
    key = core.load("job1")["repo"]["deploy_key"]
    assert core.load("job2")["repo"]["deploy_key"] == key
    assert list(fake_github.keys["ada/proj"]) == [key["id"]]  # (so GitHub sent one email, not two)
    assert machines.uploads["job1"]["deploy_key"] == machines.uploads["job2"]["deploy_key"]


def test_a_deploy_key_is_not_shared_after_a_day(fake_github):
    go(args("job1"))
    rec = core.load("job1")
    rec["repo"]["deploy_key"]["created"] = "2026-01-01T00:00:00Z"
    core.save(rec)
    go(args("job2"))
    assert core.load("job2")["repo"]["deploy_key"]["id"] != rec["repo"]["deploy_key"]["id"]
    assert len(fake_github.keys["ada/proj"]) == 2


def test_a_deploy_key_removed_on_github_is_not_shared(fake_github):
    go(args("job1"))
    fake_github.keys["ada/proj"].clear()  # (the user removed it in the repo's settings)
    go(args("job2"))
    assert list(fake_github.keys["ada/proj"]) == [core.load("job2")["repo"]["deploy_key"]["id"]]


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
    assert rec["phase"] == "ready" and list(fake_github.keys[slug]) == [rec["repo"]["deploy_key"]["id"]]
    assert fake_github.branch_sha(slug, "agent/job1") == rec["repo"]["start_sha"] == main
    assert any(c[3] == "user/repos" and "private=true" in c for c in gh_writes(fake_github))
    config = json.loads(machines.uploads["job1"]["config.json"][0])
    assert config["repo_dir"] == "/root/work/crab-student-work"


# ---- approval by price --------------------------------------------------------------------------


def test_a_machine_over_the_price_waits_and_approve_rents_up_to_that_price(fake_vast, fake_github):
    core.set_config("approve_above_usd_per_hour", "0.25")  # (both offers cost more)
    res, rec = go(args()), core.load("job1")
    assert res["exit_code"] == 3 and res["needs_approval"] and res.keys() >= RESULT_KEYS
    assert rec["phase"] == "pending" and rec["pending"]["max_dph"] == 0.35
    assert not fake_vast.calls("PUT", "/") and not gh_writes(fake_github)
    assert fake_github.branch_sha("ada/proj", "agent/job1") is None
    # approve plans again: offer 4 is over the $0.35/hr that was approved
    fake_vast.offers = [offer(3, dph_total=0.33), offer(4, dph_total=0.40)]
    res, rec = launch.approve("job1", out()), core.load("job1")
    assert res["exit_code"] == 0 and rec["phase"] == "ready" and rec["pending"] is None
    assert [o["id"] for o in rec["offers"]] == [3] and rec["max_dph"] == 0.35
    assert [r[1] for r in rents(fake_vast)] == ["/api/v0/asks/3/"]


def test_approve_asks_again_when_the_price_went_up(fake_vast):
    core.set_config("approve_above_usd_per_hour", "0.25")
    go(args())
    fake_vast.offers = [offer(5, dph_total=0.50)]
    res, rec = launch.approve("job1", out()), core.load("job1")
    assert res["exit_code"] == 3 and rec["phase"] == "pending" and rec["pending"]["max_dph"] == 0.5
    assert not rents(fake_vast)


def test_a_machine_within_the_price_goes_without_approval_and_so_do_its_backups(fake_vast):
    core.set_config("approve_above_usd_per_hour", "0.32")  # (offer 2, at $0.35, would need approval)
    res, rec = go(args()), core.load("job1")
    assert res["exit_code"] == 0 and rec["phase"] == "ready" and [o["id"] for o in rec["offers"]] == [1]


def test_decline_forgets_a_pending_launch(fake_vast):
    core.set_config("approve_above_usd_per_hour", "0.25")
    go(args())
    launch.decline("job1")
    assert not core.record_path("job1").exists() and not launch.brief_file("job1").exists()
    assert not fake_vast.calls("PUT", "/")


# ---- several at once -----------------------------------------------------------------------------


def four_offers(fake_vast) -> None:
    fake_vast.offers = [offer(i, dph_total=0.30 + i / 100) for i in (1, 2, 3, 4)]


def test_a_batch_launches_each_agent_on_a_machine_of_its_own(fake_vast, fake_github, machines):
    four_offers(fake_vast)
    res = launch.launch_batch([args("job1"), args("job2")], out())
    recs = [core.load(n) for n in ("job1", "job2")]
    assert res["exit_code"] == 0 and [r["state"] for r in res["results"]] == ["working", "working"]
    assert all(r["phase"] == "ready" for r in recs) and len(res["result_line"].splitlines()) == 2
    picks = [fake_vast.instances[r["instance_id"]]["offer_id"] for r in recs]
    assert picks == [1, 2]
    assert set(machines.uploads) == {"job1", "job2"} and len(fake_github.keys["ada/proj"]) == 1
    assert recs[0]["repo"]["start_sha"] == recs[1]["repo"]["start_sha"]  # (one snapshot for the batch)


def test_in_a_batch_only_agents_over_the_price_wait_for_approval(fake_vast):
    core.set_config("approve_above_usd_per_hour", "0.315")  # (only offer 1, at $0.31, is within)
    four_offers(fake_vast)
    res = launch.launch_batch([args("job1"), args("job2")], out())
    assert res["exit_code"] == 3 and [r["state"] for r in res["results"]] == ["working", "approval"]
    assert core.load("job2")["phase"] == "pending" and len(rents(fake_vast)) == 1


def test_approving_several_gives_each_a_machine_of_its_own(fake_vast):
    core.set_config("approve_above_usd_per_hour", "0.25")
    four_offers(fake_vast)
    launch.launch_batch([args("job1"), args("job2")], out())
    res = launch.approve_all(["job1", "job2"], out())
    recs = [core.load(n) for n in ("job1", "job2")]
    assert res["exit_code"] == 0 and all(r["phase"] == "ready" for r in recs)
    assert len({fake_vast.instances[r["instance_id"]]["offer_id"] for r in recs}) == 2


def test_one_failed_launch_in_a_batch_leaves_the_others_running(fake_vast, machines, monkeypatch):
    four_offers(fake_vast)
    real = launch.setup_machine

    def setup_machine(rec, progress):
        if rec["name"] == "job2":
            raise core.CrabError("setup failed at step 4 (Claude Code): the token was refused", 5)
        real(rec, progress)

    monkeypatch.setattr(launch, "setup_machine", setup_machine)
    res = launch.launch_batch([args("job1"), args("job2")], out())
    assert res["exit_code"] == 5 and [r["state"] for r in res["results"]] == ["working", "failed"]
    assert core.load("job1")["phase"] == "ready"


@pytest.mark.parametrize("problem", ["same name", "taken name", "no machine", "too few machines"])
def test_a_batch_that_cannot_be_planned_rents_nothing(problem, fake_vast):
    items = [args("job1"), args("job1" if problem == "same name" else "job2")]
    if problem == "taken name":
        core.save({"name": "job2", "phase": "pending"})
    if problem == "no machine":
        items[1] = args("job2", req=offers.Req(hours=1, gpus=8))
    if problem == "too few machines":
        fake_vast.offers = [offer(1)]
    fails(4 if "machine" in problem else 2, launch.launch_batch, items, out())
    assert not rents(fake_vast) and not core.record_path("job1").exists()


# ---- the rent loop -------------------------------------------------------------------------------


def test_a_taken_offer_moves_on_to_the_next(fake_vast):
    fake_vast.rent_outcome[1] = "taken"
    go(args())
    rec = core.load("job1")
    assert rec["phase"] == "ready" and fake_vast.instances[rec["instance_id"]]["offer_id"] == 2
    assert [f["id"] for f in rec["failed_offers"]] == [1] and len(rents(fake_vast)) == 2


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
    """The agent may be at work there: its machine is started again and kept, and setup runs again."""
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
    core.set_config("approve_above_usd_per_hour", "0.25")
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


# ---- start ---------------------------------------------------------------------------------------


def test_start_has_no_time_limit_and_never_waits_for_approval(fake_vast, machines):
    go(args())
    stop_machine(fake_vast)
    rec = core.load("job1")
    rec["last_seen"] = {"used_hours": 500.0, "stop_reason": "idle"}
    core.save(rec)
    core.set_config("approve_above_usd_per_hour", "0")
    res = launch.start("job1", out())
    assert res["state"] == "working" and res["exit_code"] == 0 and res.keys() >= RESULT_KEYS
    assert machine_runs(fake_vast) and len(starts(fake_vast)) == 1 and not machines.calls


def test_start_waits_while_the_machine_reads_exited_and_forgets_why_it_stopped(fake_vast, machines):
    go(args())
    stop_machine(fake_vast)
    rec = core.load("job1")
    rec["last_seen"] = {"used_hours": 0.5, "stop_reason": "user"}
    core.save(rec)
    fake_vast.start_polls = 3  # (it reads "exited", with an error, until its host has room)
    assert launch.start("job1", out())["state"] == "working" and machine_runs(fake_vast)
    assert core.load("job1")["last_seen"]["stop_reason"] is None and not fake_vast.calls("DELETE", "/")


def test_a_failed_launch_tried_again_asks_first_if_only_pricier_machines_are_left(fake_vast, machines):
    machines.setup_result = core.CrabError("setup failed at step 2 (repository): clone failed", 5)
    fails(5, go, args())
    machines.setup_result = None
    fake_vast.offers = [offer(9, dph_total=7.50)]  # (over the default $5/hr)
    res = launch.start("job1", out())
    assert res["exit_code"] == 3 and core.load("job1")["phase"] == "pending" and not machine_runs(fake_vast)
    launch.approve("job1", out())
    assert core.load("job1")["phase"] == "ready" and core.load("job1")["max_dph"] == 7.5
