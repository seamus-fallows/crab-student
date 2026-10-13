"""Launching through the fakes: the happy path and the repo, the mode and Claude settings, the approval gate, the
rent loop's failure modes, interrupted launches, and start."""

from __future__ import annotations

import io
import json
import re
import subprocess
import time
from pathlib import Path

import pytest
from fake_gh import git
from fake_vast import offer

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


def args(name: str = "job1", offer_ids=(1, 2), **kw) -> launch.LaunchArgs:
    fields = {"disk_gb": 64, "brief": "# Task\nDo the thing.\n", "repo_path": None, "no_repo": False,
              "secrets": [], "fake_claude": False, "dry_run": False}  # fmt: skip
    return launch.LaunchArgs(name=name, offer_ids=list(offer_ids), **{**fields, **kw})


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


def test_a_launch_races_two_machines_keeps_the_first_ready_and_sets_it_up(
    fake_vast, fake_github, user_repo, machines, monkeypatch, capsys
):
    recorded = []  # (the label sent, the record's rent labels at the moment Vast gets the rent request)
    real_rent = fake_vast._rent

    def rent(offer_id, body):
        recorded.append((body["label"], [r["label"] for r in core.load("job1")["rents"]]))
        return real_rent(offer_id, body)

    monkeypatch.setattr(fake_vast, "_rent", rent)
    monkeypatch.setattr("sys.stdin", io.StringIO("# Task\nDo the thing.\n"))
    code = cli.main(["launch", "job1", "--offers", "1,2", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    res, rec = json.loads(captured.out), core.load("job1")
    assert res["state"] == "working" and res["branch"] == "agent/job1" and res.keys() >= RESULT_KEYS
    assert rec["phase"] == "ready" and res["instance_id"] == rec["instance_id"]
    assert [r[1] for r in rents(fake_vast)] == ["/api/v0/asks/1/", "/api/v0/asks/2/"]  # (both at once)
    assert all(label in saved for label, saved in recorded) and len(recorded) == 2
    assert all(launch.own_label(label) and label.startswith("crab-student-job1-") for label, _ in recorded)
    # the first machine ready (offer 1) is kept; the other was destroyed before anything reached it
    assert fake_vast.instances[rec["instance_id"]]["offer_id"] == 1 and list(fake_vast.instances) == [
        rec["instance_id"]
    ]
    assert len(fake_vast.calls("DELETE", "/")) == 1 and set(machines.uploads) == {"job1"}
    assert rec["rent"]["label"] == recorded[0][0] and rec["rents"] == [] == rec["failed_offers"]
    assert rec["last_start"] and rec["disk_gb"] == 64 and "limit_hours" not in rec
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


def test_a_launch_rents_the_offers_it_is_given_in_their_order(fake_vast):
    three_offers(fake_vast)
    go(args(offer_ids=[3, 1]))
    assert [r[1] for r in rents(fake_vast)] == ["/api/v0/asks/3/", "/api/v0/asks/1/"]
    assert [o["id"] for o in core.load("job1")["offers"]] == [3, 1]


def test_offers_that_can_no_longer_be_rented_are_skipped_and_none_left_is_exit_4(fake_vast, fake_github):
    fake_vast.offers = [offer(1, dph_total=0.30), offer(2, rentable=False), offer(3, cuda_max_good=11.0)]
    go(args(offer_ids=[9, 2, 3, 1]))  # (9 is gone, 2 taken, 3 fails the fixed rules)
    assert [o["id"] for o in core.load("job1")["offers"]] == [1]
    assert [r[1] for r in rents(fake_vast)] == ["/api/v0/asks/1/"]
    fails(4, go, args("job2", offer_ids=[9, 2]))
    assert not core.record_path("job2").exists() and len(rents(fake_vast)) == 1
    assert fake_github.branch_sha("ada/proj", "agent/job2") is None


@pytest.mark.parametrize("ids", [[], [1, 1], [1, 2, 3, 4, 5, 6]])
def test_offer_lists_that_make_no_sense_are_usage_errors(ids, fake_vast):
    fails(2, go, args(offer_ids=ids))
    assert not rents(fake_vast) and not core.record_path("job1").exists()


def test_two_offers_on_one_machine_are_refused(fake_vast):
    fake_vast.offers = [offer(1, machine_id=7), offer(2, machine_id=7)]
    fails(2, go, args())
    assert not rents(fake_vast) and not core.record_path("job1").exists()


def test_the_rent_asks_for_the_disk_given(fake_vast):
    go(args(offer_ids=[1], disk_gb=120))
    (sent,) = rents(fake_vast)
    assert sent[2]["disk"] == 120.0 and core.load("job1")["disk_gb"] == 120
    assert "NVIDIA_DISABLE_REQUIRE" not in sent[2]["env"]  # (every offer has a driver for the image's CUDA)
    (search,) = fake_vast.calls("POST", "/api/v0/bundles/")
    assert search[2]["allocated_storage"] == 120  # (the price the launch checks includes that disk)


def test_the_owner_setting_starts_the_rent_labels(fake_vast):
    core.set_config("owner", "SFALLOWS")
    go(args(offer_ids=[1, 2]))
    labels = [sent[2]["label"] for sent in rents(fake_vast)]
    assert all(re.fullmatch(rf"SFALLOWS-job1-{core.install_id()}-[0-9a-f]{{4}}", x) for x in labels)
    assert len(set(labels)) == 2 and all(launch.own_label(x) for x in labels)
    fails(2, core.set_config, "owner", "S FALLOWS")


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


def test_agents_on_a_repo_share_one_deploy_key(fake_github, machines):
    go(args("job1"))
    go(args("job2"))
    key = core.load("job1")["repo"]["deploy_key"]
    assert core.load("job2")["repo"]["deploy_key"] == key
    assert list(fake_github.keys["ada/proj"]) == [key["id"]]  # (so GitHub sent one email, not two)
    assert machines.uploads["job1"]["deploy_key"] == machines.uploads["job2"]["deploy_key"]


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


def test_committed_only_leaves_the_uncommitted_changes_behind(fake_github, user_repo):
    (user_repo / "README.md").write_text("changed\n")
    go(args(committed_only=True))
    rec, bare = core.load("job1"), fake_github.bare("ada/proj")
    assert (
        rec["repo"]["start_sha"] == git(user_repo, "rev-parse", "HEAD").strip() and not rec["repo"]["carried"]
    )
    assert git(bare, "rev-parse", "agent/job1").strip() == rec["repo"]["start_sha"]
    assert "changed" not in git(bare, "show", "agent/job1:README.md")


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


def test_a_pick_over_the_price_waits_and_approve_rents_up_to_the_highest_price_listed(fake_vast, fake_github):
    core.set_config("approve_above_usd_per_hour", "0.25")  # (both offers cost more)
    res, rec = go(args()), core.load("job1")
    assert res["exit_code"] == 3 and res["needs_approval"] and res.keys() >= RESULT_KEYS
    assert rec["phase"] == "pending" and rec["pending"]["max_dph"] == 0.35
    assert not fake_vast.calls("PUT", "/") and not gh_writes(fake_github)
    assert fake_github.branch_sha("ada/proj", "agent/job1") is None
    # approve looks again: offer 2 now costs more than the $0.35/hr that was approved
    fake_vast.offers = [offer(1, dph_total=0.30), offer(2, dph_total=0.40)]
    res, rec = launch.approve("job1", out()), core.load("job1")
    assert res["exit_code"] == 0 and rec["phase"] == "ready" and rec["pending"] is None
    assert [o["id"] for o in rec["offers"]] == [1] and rec["max_dph"] == 0.35
    assert [r[1] for r in rents(fake_vast)] == ["/api/v0/asks/1/"]


def test_approve_asks_again_when_the_price_went_up(fake_vast):
    core.set_config("approve_above_usd_per_hour", "0.25")
    go(args())
    fake_vast.offers = [offer(1, dph_total=0.50), offer(2, dph_total=0.45)]
    res, rec = launch.approve("job1", out()), core.load("job1")
    assert res["exit_code"] == 3 and rec["phase"] == "pending" and rec["pending"]["max_dph"] == 0.5
    assert not rents(fake_vast)


def test_approve_when_the_machines_are_gone_rents_nothing(fake_vast):
    core.set_config("approve_above_usd_per_hour", "0.25")
    go(args())
    fake_vast.offers = []
    fails(4, launch.approve, "job1", out())
    assert core.load("job1")["phase"] == "pending" and not rents(fake_vast)


def test_backups_over_the_price_are_left_out_when_the_pick_is_within_it(fake_vast):
    core.set_config("approve_above_usd_per_hour", "0.32")  # (offer 2, at $0.35, is over it)
    res, rec = go(args()), core.load("job1")
    assert res["exit_code"] == 0 and rec["phase"] == "ready" and [o["id"] for o in rec["offers"]] == [1]
    assert [r[1] for r in rents(fake_vast)] == ["/api/v0/asks/1/"]


def test_a_pick_over_the_price_waits_even_when_a_backup_is_within_it(fake_vast):
    core.set_config("approve_above_usd_per_hour", "0.32")
    res, rec = go(args(offer_ids=[2, 1])), core.load("job1")
    assert res["exit_code"] == 3 and rec["phase"] == "pending" and rec["pending"]["max_dph"] == 0.35
    assert [o["id"] for o in rec["offers"]] == [2, 1] and not rents(fake_vast)


def test_decline_forgets_a_pending_launch(fake_vast):
    core.set_config("approve_above_usd_per_hour", "0.25")
    go(args())
    launch.decline("job1")
    assert not core.record_path("job1").exists() and not launch.brief_file("job1").exists()
    assert not fake_vast.calls("PUT", "/")


# ---- the race ------------------------------------------------------------------------------------


def three_offers(fake_vast) -> None:
    fake_vast.offers = [offer(1, dph_total=0.30), offer(2, dph_total=0.32), offer(3, dph_total=0.34)]


def kept_offer(fake_vast) -> int:
    rec = core.load("job1")
    assert rec["phase"] == "ready" and list(fake_vast.instances) == [
        rec["instance_id"]
    ]  # (nothing else left)
    return fake_vast.instances[rec["instance_id"]]["offer_id"]


def test_the_machine_ready_first_wins_the_race_even_if_it_is_the_backup(fake_vast):
    fake_vast.boot[1] = 6  # (polls until it runs: offer 2 runs at once)
    go(args())
    assert kept_offer(fake_vast) == 2 and len(rents(fake_vast)) == 2
    assert core.load("job1")["failed_offers"] == []  # (offer 1 lost the race: it did not fail)


def test_a_taken_offer_moves_on_to_the_next(fake_vast):
    fake_vast.rent_outcome[1] = "taken"
    go(args())
    assert kept_offer(fake_vast) == 2 and [f["id"] for f in core.load("job1")["failed_offers"]] == [1]
    assert len(rents(fake_vast)) == 2


@pytest.mark.parametrize("boot", ["boot_error", "exits"])
def test_a_machine_that_fails_to_start_is_destroyed_and_the_next_offer_rented_in_its_place(boot, fake_vast):
    three_offers(fake_vast)
    fake_vast.boot[1], fake_vast.boot[2] = boot, "never"
    go(args(offer_ids=[1, 2, 3]))
    assert kept_offer(fake_vast) == 3 and [r[1] for r in rents(fake_vast)][-1] == "/api/v0/asks/3/"
    assert [f["id"] for f in core.load("job1")["failed_offers"]] == [1]  # (offer 2 lost the race to 3)
    assert len(fake_vast.calls("DELETE", "/")) == 2


def test_machines_not_started_within_the_start_time_are_given_up(fake_vast, monkeypatch):
    monkeypatch.setattr(launch, "START_TIMEOUT", 0.3)  # (5 minutes for real)
    three_offers(fake_vast)
    fake_vast.boot[1] = fake_vast.boot[2] = "never"
    go(args(offer_ids=[1, 2, 3]))
    assert kept_offer(fake_vast) == 3 and len(rents(fake_vast)) == 3
    assert sorted(f["id"] for f in core.load("job1")["failed_offers"]) == [1, 2]


def test_a_running_machine_whose_ssh_does_not_answer_in_time_is_given_up(fake_vast, monkeypatch):
    monkeypatch.setattr(launch, "SSH_TIMEOUT", 0.3)
    three_offers(fake_vast)
    fake_vast.boot[1], fake_vast.boot[2] = "no_ssh", "never"
    go(args(offer_ids=[1, 2, 3]))
    assert kept_offer(fake_vast) == 3 and [f["id"] for f in core.load("job1")["failed_offers"]] == [1]


def test_never_more_than_two_machines_are_in_flight(fake_vast, monkeypatch):
    monkeypatch.setattr(launch, "START_TIMEOUT", 0.3)
    three_offers(fake_vast)
    fake_vast.boot.update({1: "never", 2: "never"})
    most, real_rent = [], fake_vast._rent

    def rent(offer_id, body):
        most.append(len(fake_vast.instances) + 1)
        return real_rent(offer_id, body)

    monkeypatch.setattr(fake_vast, "_rent", rent)
    go(args(offer_ids=[1, 2, 3]))
    assert max(most) <= launch.RACERS and core.load("job1")["phase"] == "ready"


def test_a_running_machine_with_an_old_error_in_its_status_is_kept(fake_vast, monkeypatch):
    fake_vast.offers = [offer(1)]
    fake_vast.boot[1] = "stale_error"
    answers = iter([False, False])  # (ssh answers from the third poll)
    monkeypatch.setattr(machine, "reachable", lambda name, addr=None: next(answers, True))
    go(args(offer_ids=[1]))
    assert kept_offer(fake_vast) == 1 and len(rents(fake_vast)) == 1 and not fake_vast.calls("DELETE", "/")


def test_when_every_offer_fails_the_launch_fails_with_exit_4(fake_vast):
    fake_vast.rent_outcome[1] = "taken"
    fake_vast.boot[2] = "exits"
    fails(4, go, args())
    rec = core.load("job1")
    assert rec["phase"] == "failed" and rec["error"] and rec["instance_id"] is None and rec["rents"] == []
    assert not fake_vast.instances  # (the one that did not start was destroyed)


def test_after_every_offer_failed_start_needs_other_offers(fake_vast, machines):
    fake_vast.rent_outcome[1] = fake_vast.rent_outcome[2] = "taken"
    fails(4, go, args())
    fails(4, launch.start, "job1", out())  # (none of its own offers is left)
    assert len(rents(fake_vast)) == 2
    fake_vast.offers.append(offer(3, dph_total=0.32))
    assert launch.start("job1", out(), [3])["state"] == "working" and kept_offer(fake_vast) == 3
    fails(2, launch.start, "job1", out(), [3])  # (set up: --offers is only for a launch that failed)


@pytest.mark.parametrize("outcome", ["error_created", "slow_created"])
def test_a_rent_vast_did_not_confirm_is_found_by_its_label(outcome, fake_vast, monkeypatch):
    monkeypatch.setattr(vast, "RENT_TIMEOUT", 0.5)
    monkeypatch.setattr(launch, "LABEL_WAIT", 5.0)  # (the search stops as soon as the label shows)
    fake_vast.offers = [offer(1)]
    fake_vast.slow_seconds = 1.5
    fake_vast.rent_outcome[1] = outcome
    go(args(offer_ids=[1]))
    rec = core.load("job1")
    assert kept_offer(fake_vast) == 1 and len(rents(fake_vast)) == 1
    assert fake_vast.instances[rec["instance_id"]]["label"] == rec["rent"]["label"]


def test_a_race_left_with_only_an_unconfirmed_rent_fails_and_start_plans_afresh(fake_vast):
    fake_vast.offers = [offer(1)]
    fake_vast.rent_outcome[1] = "error_none"
    error = fails(1, go, args(offer_ids=[1]))
    rec, (sent,) = core.load("job1"), rents(fake_vast)
    assert rec["phase"] == "failed" and rec["error"] and rec["rents"] == []
    assert sent[2]["label"] in error.message  # (the machine to look out for)
    fake_vast.rent_outcome[1] = "ok"
    assert launch.start("job1", out())["state"] == "working"
    first, again = rents(fake_vast)
    assert core.load("job1")["phase"] == "ready" and again[2]["label"] != first[2]["label"]  # (a new rent)


def test_an_unconfirmed_rent_the_winner_left_behind_is_a_warning_and_list_points_out_its_machine(
    fake_vast, machines, capsys
):
    fake_vast.rent_outcome[1] = "error_none"  # (Vast did not answer: it may yet show a machine)
    res = go(args())
    label = rents(fake_vast)[0][2]["label"]
    assert kept_offer(fake_vast) == 2 and res["exit_code"] == 0 and any(label in w for w in res["warnings"])
    late = fake_vast.add_instance(1, label, status="loading")  # (it did rent, after all)
    assert cli.main(["list", "--json"]) == 0
    shown, err = capsys.readouterr()
    assert [v["name"] for v in json.loads(shown)] == ["job1"] and label in err and str(late) in err
    assert (
        late in fake_vast.instances and len(rents(fake_vast)) == 2
    )  # (never destroyed by list, never sent again)


def test_a_github_step_that_fails_during_the_race_destroys_both_machines(fake_vast, monkeypatch):
    def refused(*a):
        raise core.CrabError("GitHub refused the branch")

    monkeypatch.setattr(github, "create_branch", refused)
    fails(1, go, args())
    rec = core.load("job1")
    assert rec["phase"] == "failed" and rec["rents"] == [] and len(rents(fake_vast)) == 2
    assert not fake_vast.instances  # (neither held anything)


def test_no_credit_fails_the_launch_without_trying_other_offers(fake_vast):
    fake_vast.rent_outcome[1] = "no_credit"
    assert "credit" in fails(1, go, args()).message
    assert core.load("job1")["phase"] == "failed" and len(rents(fake_vast)) == 1 and not fake_vast.instances


# ---- interruptions and setup failures ------------------------------------------------------------


def interrupt_once(monkeypatch, owner, name: str) -> None:
    """The next call of owner.name is cut off (Ctrl-C, a closed terminal); later calls work."""
    real = getattr(owner, name)

    def interrupted(*a, **kw):
        monkeypatch.setattr(owner, name, real)
        raise KeyboardInterrupt

    monkeypatch.setattr(owner, name, interrupted)


@pytest.mark.parametrize("where,phase", [("race", "renting"), ("kept", "booting")])
def test_a_launch_cut_off_before_its_upload_is_destroyed_and_planned_afresh_by_start(
    where, phase, fake_vast, machines, monkeypatch
):
    interrupt_once(monkeypatch, *((machine, "reachable") if where == "race" else (launch, "setup_machine")))
    with pytest.raises(KeyboardInterrupt):
        go(args())
    rec, rented = core.load("job1"), set(fake_vast.instances)
    assert rec["phase"] == phase and len(rec["rents"]) == (2 if where == "race" else 0) and rented
    assert launch.start("job1", out())["state"] == "working"
    rec = core.load("job1")
    assert rec["phase"] == "ready" and len(rents(fake_vast)) == 4  # (two fresh rents)
    assert list(fake_vast.instances) == [rec["instance_id"]] and not rented & set(fake_vast.instances)
    assert "job1" in machines.uploads


def test_a_launch_cut_off_during_its_upload_is_continued_by_start(fake_vast, machines):
    machines.setup_result = machine.Unreachable("could not reach job1 over ssh for the upload")
    fails(1, go, args())
    machines.setup_result, machines.uploads = None, {}
    rec = core.load("job1")
    assert rec["phase"] == "setup" and rec["instance_id"] in fake_vast.instances
    assert launch.start("job1", out())["state"] == "working"
    assert core.load("job1")["phase"] == "ready" and len(rents(fake_vast)) == 2  # (nothing rented again)
    assert list(fake_vast.instances) == [rec["instance_id"]] and "job1" in machines.uploads


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


def test_a_failed_setup_destroys_the_machine_and_start_tries_its_offers_again(fake_vast, machines):
    machines.setup_result = core.CrabError("setup failed at step 4 (Claude Code): the token was refused", 5)
    fails(5, go, args())
    rec = core.load("job1")
    assert rec["phase"] == "failed" and rec["instance_id"] is None and not fake_vast.instances
    machines.setup_result = None
    assert launch.start("job1", out())["state"] == "working"
    rec = core.load("job1")
    assert (
        rec["phase"] == "ready"
        and len(rents(fake_vast)) == 4
        and list(fake_vast.instances) == [rec["instance_id"]]
    )


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
    real_named, other = offers.named, {"name": "job1", "phase": "pending"}

    def named(*a, **kw):
        core.save(other)  # (the other launch passes the gate first)
        return real_named(*a, **kw)

    monkeypatch.setattr(offers, "named", named)
    fails(2, go, args())
    assert core.load("job1") == other and not fake_vast.calls("PUT", "/") and not gh_writes(fake_github)


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


def test_start_keeps_what_the_last_session_and_the_stop_cost(fake_vast, machines):
    go(args())
    stop_machine(fake_vast)
    rec = core.load("job1")
    assert (rec["past_hours"], rec["stopped_hours"], rec["fetched"]) == (0, 0, None)
    rec["last_seen"] = {"used_hours": 2.5, "stop_reason": "idle"}
    rec["stopped_since"] = time.time() - 3 * 3600
    core.save(rec)
    launch.start("job1", out())
    rec = core.load("job1")
    assert rec["past_hours"] == 2.5 and rec["stopped_hours"] == pytest.approx(3.0, abs=0.01)
    assert rec["last_seen"]["used_hours"] == 0 and rec["stopped_since"] is None


def test_start_waits_while_the_machine_reads_exited_and_forgets_why_it_stopped(fake_vast, machines):
    go(args())
    stop_machine(fake_vast)
    rec = core.load("job1")
    rec["last_seen"] = {"used_hours": 0.5, "stop_reason": "user"}
    core.save(rec)
    fake_vast.start_polls = 3  # (it reads "exited", with an error, until its host has room)
    destroyed = len(fake_vast.calls("DELETE", "/"))  # (the machine that lost the launch's race)
    assert launch.start("job1", out())["state"] == "working" and machine_runs(fake_vast)
    assert core.load("job1")["last_seen"]["stop_reason"] is None
    assert (
        len(fake_vast.calls("DELETE", "/")) == destroyed
        and core.load("job1")["instance_id"] in fake_vast.instances
    )


def test_a_failed_launch_tried_again_on_a_pricier_machine_asks_first(fake_vast, machines):
    machines.setup_result = core.CrabError("setup failed at step 2 (repository): clone failed", 5)
    fails(5, go, args())
    machines.setup_result = None
    fake_vast.offers.append(offer(9, dph_total=7.50))  # (over the default $5/hr)
    res = launch.start("job1", out(), [9])
    assert res["exit_code"] == 3 and core.load("job1")["phase"] == "pending" and not machine_runs(fake_vast)
    launch.approve("job1", out())
    assert core.load("job1")["phase"] == "ready" and core.load("job1")["max_dph"] == 7.5


# ---- progress: the launch log and the window that follows it --------------------------------------


def log_lines(name: str = "job1") -> list[str]:
    return launch.log_file(name).read_text(encoding="utf-8").splitlines()


def test_every_progress_line_goes_to_the_agents_log_with_a_time(fake_vast):
    fake_vast.boot[1] = 3
    shown = io.StringIO()
    launch.launch(args(), launch.Progress(shown))
    logged = log_lines()
    assert all(re.fullmatch(r"\d\d:\d\d:\d\d .+", line) for line in logged)
    texts = [line[9:] for line in logged]
    for line in shown.getvalue().splitlines():  # (status lines too, marked "~")
        assert line in texts or f"~ {line}" in texts, line


def test_a_status_line_is_printed_when_it_moves_on_and_a_new_detail_at_most_every_15_seconds(monkeypatch):
    shown, clock = io.StringIO(), [0.0]
    with monkeypatch.context() as m:
        m.setattr(launch.time, "monotonic", lambda: clock[0])
        progress = launch.Progress(shown)
        for t, detail in [(1, " - layer 1/9"), (5, " - layer 2/9"), (17, " - layer 3/9"), (18, ""), (40, "")]:
            clock[0] = t
            progress.status(f"Booting: {'loading' if detail else 'running'}{detail}")
    printed = [line.rsplit(" (", 1)[0] for line in shown.getvalue().splitlines()]
    assert printed == ["Booting: loading - layer 1/9", "Booting: loading - layer 3/9", "Booting: running"]


def test_a_failed_launch_logs_the_error_and_what_to_do(fake_vast):
    fake_vast.rent_outcome[1] = fake_vast.rent_outcome[2] = "taken"
    error = fails(4, go, args())
    assert any(error.message in line for line in log_lines())


def test_a_launch_opens_a_window_that_follows_it_unless_turned_off(fake_vast, windows):
    go(args("job1"))
    core.set_config("open_window", "off")
    shown = io.StringIO()
    launch.launch(args("job2"), launch.Progress(shown))
    assert windows == ["job1"] and "crab-student open job2" in shown.getvalue()


def test_no_window_for_a_launch_that_waits_for_approval_or_a_dry_run(fake_vast, windows):
    core.set_config("approve_above_usd_per_hour", "0.25")
    go(args("job1"))
    go(args("job2", dry_run=True))
    assert windows == []
    core.set_config("approve_above_usd_per_hour", "5")
    launch.approve("job1", out())
    assert windows == ["job1"]


def test_the_dependency_setup_and_the_users_vs_code_reach_the_machine(machines, monkeypatch):
    commit = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr(launch.machine, "local_vscode", lambda: commit)
    go(args(setup="pip install -r requirements-gpu.txt", mode="interactive"))
    config = json.loads(machines.uploads["job1"]["config.json"][0])
    assert core.load("job1")["setup"] == config["setup"] == "pip install -r requirements-gpu.txt"
    assert config["vscode_commit"] == commit
    go(args("job2"))  # (a colleague: VS Code installs its own server if it is ever opened)
    config = json.loads(machines.uploads["job2"]["config.json"][0])
    assert config["setup"] is None and config["vscode_commit"] is None  # (detected on the machine)


def test_only_a_fake_claude_launch_uploads_the_fake_claude(machines):
    go(args("job1"))
    go(args("job2", fake_claude=True))
    assert "fake_claude.py" not in machines.uploads["job1"] and "fake_claude.py" in machines.uploads["job2"]
