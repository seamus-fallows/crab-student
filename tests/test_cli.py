"""The command line: exit codes, stdout and stderr, --json, ASCII, and what each command hands its layer."""

from __future__ import annotations

import dataclasses
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_vast import cpu_offer, offer

from crab_student import __version__, agents, cli, core, github, launch, machine, setup

VIEW_KEYS = {f.name for f in dataclasses.fields(agents.View)}


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def view(name: str = "job1", **fields) -> agents.View:
    base = {"name": name, "state": "working", "message": "epoch 3/10, val loss 0.41", "used_h": 1.2,
            "spent": 0.51, "idle_min": 2, "dph": 0.42, "repo": "ada/proj", "mode": "colleague", "frontend": "tmux",
            "settings": {"model": "opus", "effortLevel": "high"}, "error": None,
            "machine": "1x RTX 4090 24 GB, 16 cores, Quebec, CA", "branch": f"agent/{name}",
            "git": {"dirty": 0, "unpushed": 0}, "vast_status": "running", "pending": None, "stopped_h": None,
            "disk_usd_day": None}  # fmt: skip
    return agents.View(**{**base, **fields})


def record(name: str = "job1", **fields) -> dict:
    return {"name": name, "created": core.now_iso(), "phase": "ready", "pending": None, "instance_id": 5001,
            "repo": {"slug": "ada/proj-agents", "source": "ada/proj", "branch": f"agent/{name}",
                     "dir": "/root/work/proj"},
            "offers": [], "dph": 0.42, "short": "1x RTX 4090 (Quebec, CA)", "mode": "colleague",
            "claude_settings": {}, "idle_minutes": 60, "disk_gb": 64, **fields}  # fmt: skip


def result(state: str = "working", code: int = 0) -> dict:
    return {"name": "job1", "state": state, "result_line": f"job1: {state}", "warnings": [],
            "needs_approval": code == 3, "exit_code": code}  # fmt: skip


@pytest.fixture
def patched(monkeypatch) -> list[tuple[str, tuple]]:
    """The layers under the CLI replaced by stand-ins that record their calls; agent job1 exists."""
    calls: list[tuple[str, tuple]] = []

    def stand_in(module, name: str, value: object) -> None:
        def call(*args):
            calls.append((name, args))
            return value

        monkeypatch.setattr(module, name, call)

    for name in ("approve", "start"):
        stand_in(launch, name, result())
    stand_in(launch, "decline", "Declined job1: nothing was rented.")
    for name in ("stop", "delete", "send"):
        stand_in(agents, name, {"name": "job1", "result_line": f"{name}: done"})
    stand_in(agents, "report", "# Report\n")
    stand_in(agents, "listing", ([view()], []))
    stand_in(agents, "view", view())
    stand_in(github, "pull", "Fetched agent/job1.")
    core.save(record())
    return calls


@pytest.fixture
def launched(monkeypatch) -> list[launch.LaunchArgs]:
    """launch.launch replaced: the LaunchArgs it was given (a brief waits on stdin)."""
    got: list[launch.LaunchArgs] = []
    monkeypatch.setattr(launch, "launch", lambda a, out: got.append(a) or result())
    monkeypatch.setattr("sys.stdin", io.StringIO("# Task\nDo the thing.\n"))
    return got


def exit_code(argv: list[str]) -> int:
    try:
        return cli.main(argv)
    except SystemExit as e:  # (argparse: --version, and arguments it cannot parse)
        return e.code


@pytest.mark.parametrize(
    "exc, code",
    [*((core.CrabError("café", c), c) for c in range(1, 6)), (ValueError(), 1), (KeyboardInterrupt(), 1)],
)
def test_failures_go_to_stderr_with_their_exit_code(exc, code, monkeypatch, capsys):
    def fail(name):
        raise exc

    monkeypatch.setattr(agents, "report", fail)
    monkeypatch.delenv("CRAB_STUDENT_DEBUG", raising=False)  # (with it, an unexpected error is raised)
    rc, out, err = run(capsys, "report", "job1")
    assert (rc, out) == (code, "") and err.startswith(("error:", "interrupted")) and err.isascii()


@pytest.mark.parametrize(
    "argv",
    ["", "launch", "frobnicate", "extend job1 2", "open", "open job1 --all", "launch job1",
     "launch job1 --offers 1 --repo . --no-repo", "launch job1 --offers x", "launch job1 --offers 1,,2",
     "launch job1 --offers 0", "launch job1 --offers 1 --disk nan", "launch job1 --offers 1 --hours 3",
     "offers --hours inf", "offers --hours 0", "offers --gpus 4-2", "offers --gpus two", "offers --ram -1",
     "offers --limit 0", "offers --sort fastest", "launch job1 --offers 1 --brief-file no-such-brief.md",
     "launch job1 --offers 1 --mode solo", "launch job1 --offers 1 --effort max", "launch job1 --offers 1 --fast on",
     "launch --offers 1", "start job1 --offers 1,a", "approve", "approve job1 job2"],
)  # fmt: skip
def test_usage_mistakes_exit_2(argv, launched, capsys):
    assert exit_code(argv.split()) == 2 and not launched


def test_version(capsys):
    assert exit_code(["--version"]) == 0 and __version__ in capsys.readouterr().out


def test_launch_json_is_one_value_with_progress_on_stderr(monkeypatch, capsys):
    def fake_launch(a: launch.LaunchArgs, out: launch.Progress) -> dict:
        out.line("Planning job1 on a machine in Montréal")
        out.status("Booting")
        return {**result(), "machine": "Montréal"}

    monkeypatch.setattr(launch, "launch", fake_launch)
    monkeypatch.setattr("sys.stdin", io.StringIO("# Task\n"))
    rc, out, err = run(capsys, "launch", "job1", "--offers", "1", "--json")
    assert rc == 0 and len(out.splitlines()) == 1 and json.loads(out)["result_line"]
    assert "Planning job1" in err and "Booting" in err and out.isascii() and err.isascii()


@pytest.mark.parametrize(
    "argv",
    ["approve job1", "start job1", "stop job1", "delete job1", "list",
     "status job1", "config", "config regions", "config regions CA", "config regions --reset", "secret list",
     "secret delete HF_TOKEN"],
)  # fmt: skip
def test_json_prints_exactly_one_value(argv, patched, capsys):
    rc, out, _ = run(capsys, *argv.split(), "--json")
    json.loads(out)
    assert rc == 0 and len(out.splitlines()) == 1


@pytest.mark.parametrize(
    "argv, layer, args",
    [("approve job1", "approve", ("job1",)), ("decline job1", "decline", ("job1",)),
     ("start job1", "start", ("job1",)), ("stop job1", "stop", ("job1",)),
     ("delete job1 --force", "delete", ("job1", True)), ("delete job1", "delete", ("job1", False)),
     ("send job1 hello there", "send", ("job1", "hello there")), ("report job1", "report", ("job1",))],
)  # fmt: skip
def test_commands_reach_their_layer(argv, layer, args, patched, capsys):
    rc, out, _ = run(capsys, *argv.split())
    assert rc == 0 and out
    assert patched[-1][0] == layer and patched[-1][1][: len(args)] == args


def test_pull_and_a_message_on_stdin(patched, monkeypatch, capsys):
    assert cli.main(["pull", "job1", "--repo", "elsewhere"]) == 0
    rec, path = patched[-1][1]
    assert rec["name"] == "job1" and path == Path("elsewhere")
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin\n"))
    assert cli.main(["send", "job1"]) == 0
    assert patched[-1] == ("send", ("job1", "from stdin\n"))


def test_a_launch_that_needs_approval_exits_3(launched, monkeypatch, capsys):
    monkeypatch.setattr(launch, "launch", lambda a, out: result("approval", 3))
    rc, out, _ = run(capsys, "launch", "job1", "--offers", "1", "--json")
    assert rc == 3 and json.loads(out)["needs_approval"]


def test_all_output_is_ascii_but_the_agents_report(patched, monkeypatch, capsys):
    monkeypatch.setattr(agents, "listing", lambda: ([view(message="naïve ✓")], []))
    monkeypatch.setattr(agents, "view", lambda name: view(message="naïve ✓"))
    for argv in ("list", "list --json", "status job1", "status job1 --json"):
        assert cli.main(argv.split()) == 0
    out, err = capsys.readouterr()
    assert out.isascii() and err.isascii()
    monkeypatch.setattr(agents, "report", lambda name: "# Résumé\nloss 0.41\n")  # (the agent's own words)
    assert cli.main(["report", "job1"]) == 0
    assert "Résumé" in capsys.readouterr().out


def test_config_lists_gets_sets_and_resets(capsys):
    out = run(capsys, "config")[1]
    assert all(any(line.startswith(key) for line in out.splitlines()) for key in core.DEFAULTS)
    for bad in ("nope", "nope 1", "nope --reset", "idle_minutes soon", "regions Atlantis",
                "approve_above_usd_per_hour -1", "mode solo", "fast_mode maybe"):  # fmt: skip
        rc, _, err = run(capsys, "config", *bad.split())
        assert rc == 2 and err.startswith("error:") and core.load_config() == core.DEFAULTS, bad
    assert run(capsys, "config", "regions", "CA, europe")[0] == 0
    assert core.load_config()["regions"] == ["CA", "europe"]
    assert run(capsys, "config", "regions")[1].strip() == "CA, europe"
    assert json.loads(run(capsys, "config", "regions", "--json")[1]) == ["CA", "europe"]
    assert run(capsys, "config", "regions", "")[0] == 0 and core.load_config()["regions"] == []  # (anywhere)
    run(capsys, "config", "approve_above_usd_per_hour", "8")
    run(capsys, "config", "idle_minutes", "15")
    run(capsys, "config", "idle_minutes", "--reset")
    shown = json.loads(run(capsys, "config", "--json")[1])
    assert shown == {**core.DEFAULTS, "approve_above_usd_per_hour": 8.0}


def test_secrets_are_stored_listed_and_deleted_without_showing_values(monkeypatch, capsys):
    assert run(capsys, "secret", "set", "HF_TOKEN")[0] == 2  # (it asks for the value: only in a terminal)
    assert core.secret_names() == [] and core.get_secret(core.secret_key("HF_TOKEN")) is None
    monkeypatch.setattr(setup, "interactive", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "hf_value_123")
    rc, out, err = run(capsys, "secret", "set", "HF_TOKEN", "--json")
    assert rc == 0 and json.loads(out) and "hf_value_123" not in out + err
    assert core.get_secret(core.secret_key("HF_TOKEN")) == "hf_value_123"
    assert "HF_TOKEN" in run(capsys, "secret", "list")[1]
    assert json.loads(run(capsys, "secret", "list", "--json")[1]) == ["HF_TOKEN"]
    assert run(capsys, "secret", "set", "hf_token")[0] == 2  # (names are environment variables)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "  ")
    assert run(capsys, "secret", "set", "WANDB_KEY")[0] == 2
    assert run(capsys, "secret", "delete", "HF_TOKEN")[0] == 0
    assert core.secret_names() == [] and core.get_secret(core.secret_key("HF_TOKEN")) is None


def test_launch_takes_the_brief_from_stdin_or_a_file(launched, monkeypatch, tmp_path, capsys):
    assert cli.main(["launch", "job1", "--offers", "1"]) == 0
    brief = tmp_path / "brief.md"
    brief.write_text("# Task\nFrom a file, café.\n", encoding="utf-8")
    assert cli.main(["launch", "job2", "--offers", "1", "--brief-file", str(brief)]) == 0
    # Windows decodes a pipe with its ANSI code page unless told otherwise; Git Bash sends UTF-8.
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO("Café ✓\n".encode()), encoding="cp1252"))
    assert cli.main(["launch", "job3", "--offers", "1"]) == 0
    briefs = [a.brief for a in launched]
    assert briefs == ["# Task\nDo the thing.\n", "# Task\nFrom a file, café.\n", "Café ✓\n"]


def test_launch_refuses_a_missing_brief(ready, monkeypatch, capsys):
    core.write_private(core.vast_key_file(), "key\n")
    for stdin in (SimpleNamespace(isatty=lambda: True), io.StringIO(""), io.StringIO(" \n")):
        monkeypatch.setattr("sys.stdin", stdin)
        rc, _, err = run(capsys, "launch", "job1", "--offers", "1")
        assert rc == 2 and err.startswith("error:")
    assert not core.record_path("job1").exists()


def test_launch_passes_its_options_on(launched, tmp_path, capsys):
    core.set_config("disk_gb", "80")
    argv = ("launch job1 --offers 31,7,12 --secret HF_TOKEN --secret WANDB_KEY --model sonnet --mode autonomous "
            "--effort xhigh --fast")  # fmt: skip
    assert cli.main([*argv.split(), "--repo", str(tmp_path), "--dry-run", "--fake-claude"]) == 0
    a = launched[0]
    assert (a.name, a.offer_ids, a.disk_gb) == ("job1", [31, 7, 12], 80)  # (the pick first; the disk setting)
    assert cli.main(["launch", "job4", "--offers", "5", "--disk", "120"]) == 0 and launched[1].disk_gb == 120
    assert a.secrets == ["HF_TOKEN", "WANDB_KEY"] and a.repo_path == tmp_path.resolve() and not a.no_repo
    assert (a.model, a.mode, a.effort, a.fast, a.dry_run, a.fake_claude) == (
        "sonnet", "autonomous", "xhigh", True, True, True)  # fmt: skip
    assert (
        cli.main(["launch", "job2", "--offers", "1", "--setup", "uv sync --extra gpu"]) == 0
        and launched[2].setup == "uv sync --extra gpu"
    )
    assert cli.main(["launch", "job3", "--offers", "1", "--setup", " "]) == 2


def test_launch_without_options_leaves_the_agent_settings_open(launched, capsys):
    assert cli.main(["launch", "job1", "--offers", "1"]) == 0
    assert cli.main(["launch", "job2", "--offers", "1", "--no-fast"]) == 0
    a, b = launched
    assert (a.mode, a.model, a.effort, a.fast, a.setup, a.disk_gb) == (None, None, None, None, None, 64)
    assert b.fast is False


# ---- list and status -----------------------------------------------------------------------------


def test_list_fits_the_terminal_and_keeps_its_columns_apart(monkeypatch, capsys):
    long = ", ".join(["epoch 3/10 val loss 0.41"] * 6)
    views = [view("ablation-1", message=long), view("big-train", state="failed", used_h=None, spent=None),
             view("sweep", state="done", used_h=100.5, spent=150.25),
             view("resnet-rerun", state="interactive", mode="interactive", message="in VS Code, idle 12m")]  # fmt: skip
    monkeypatch.setattr(agents, "listing", lambda: (views, []))
    for width in (80, 120, 250):
        monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda *_, w=width: os.terminal_size((w, 24)))
        lines = run(capsys, "list")[1].splitlines()
        assert len(lines) == 1 + len(views) and all(len(line) < width for line in lines), width
        for column in ("MODE", "STATE", "MESSAGE", "TIME", "SPENT"):
            at = lines[0].index(column)
            assert all(line[at - 1] == " " for line in lines), (width, column)
        assert (long in lines[1]) == (width == 250)


def test_list_without_agents_and_as_json(monkeypatch, capsys):
    monkeypatch.setattr(agents, "listing", lambda: ([], []))
    assert "No agents yet" in run(capsys, "list")[1]
    stopped = view("job2", state="stopped", stopped_h=26.5, disk_usd_day=0.17)
    monkeypatch.setattr(agents, "listing", lambda: ([view(), stopped], []))
    rows = json.loads(run(capsys, "list", "--json")[1])
    assert [r["name"] for r in rows] == ["job1", "job2"] and all(set(r) == VIEW_KEYS for r in rows)
    assert (rows[1]["stopped_h"], rows[1]["disk_usd_day"]) == (26.5, 0.17)


def test_list_warns_on_stderr_about_a_machine_that_belongs_to_no_agent(fake_vast, machines, capsys):
    kept, lost = (
        f"crab-student-{name}-{core.install_id()}-{x}" for name, x in (("job1", "aaaa"), ("job2", "bbbb"))
    )
    core.save(record(instance_id=fake_vast.add_instance(1, kept), rents=[],
                     rent={"label": kept}, ssh=None, last_seen=None))  # fmt: skip
    orphan = fake_vast.add_instance(1, lost)  # (a rent Vast did not confirm)
    for argv in (["list"], ["list", "--json"]):
        rc, out, err = run(capsys, *argv)
        warned = [line for line in err.splitlines() if line.startswith("warning:")]
        assert rc == 0 and len(warned) == 1 and str(orphan) in warned[0] and lost in warned[0]
    assert [r["name"] for r in json.loads(out)] == ["job1"] and orphan in fake_vast.instances


def test_status_detail_and_a_json_result_line_that_follows_the_state(patched, monkeypatch, capsys):
    rc, out, _ = run(capsys, "status", "job1")
    assert rc == 0 and "job1" in out.splitlines()[0] and "working" in out.splitlines()[0]
    for part in (view().machine, core.usd(view().spent), "agent/job1", "ada/proj-agents", "crab-student open job1",
                 "crab-student report job1", "colleague", "opus", "high"):  # fmt: skip
        assert part in out
    assert "crab-student open job1 --vscode" not in out
    reason = "it would cost $6.00/hr, and no machine that fits costs $5.00/hr or less"
    for state, pending, expect in (("working", None, "crab-student open job1"), ("stopped", None, "stopped"),
                                   ("approval", {"reason": reason, "max_dph": 6.0}, reason)):  # fmt: skip
        core.save(record(phase="pending" if pending else "ready", pending=pending))
        monkeypatch.setattr(agents, "view", lambda name, s=state: view(state=s))
        data = json.loads(run(capsys, "status", "job1", "--json")[1])
        assert set(data) == VIEW_KEYS | {"result_line"} and expect in data["result_line"], state


def test_status_of_an_interactive_agent_in_vs_code_says_to_open_it_there(patched, monkeypatch, capsys):
    core.save(record(mode="interactive"))
    in_vscode = view(state="interactive", mode="interactive", frontend="vscode", message="in VS Code, active")
    monkeypatch.setattr(agents, "view", lambda name: in_vscode)
    assert "crab-student open job1 --vscode" in run(capsys, "status", "job1")[1]
    data = json.loads(run(capsys, "status", "job1", "--json")[1])
    assert "crab-student open job1 --vscode" in data["result_line"]


def test_status_says_how_long_an_agent_has_been_stopped_and_what_its_disk_costs(patched, monkeypatch, capsys):
    stopped = view(state="stopped", vast_status="exited", idle_min=None, stopped_h=26.5, disk_usd_day=0.17)
    monkeypatch.setattr(agents, "view", lambda name: stopped)
    lines = run(capsys, "status", "job1")[1].splitlines()
    assert any(core.fmt_hours(26.5) in line and core.usd(0.17) in line for line in lines[1:])


# ---- open ----------------------------------------------------------------------------------------


@pytest.fixture
def opened(fake_vast, machines, monkeypatch) -> list[tuple]:
    """Running agents job1 (colleague) and job2 (interactive, still in VS Code); what open hands on."""
    got: list[tuple] = []
    monkeypatch.setattr(machine, "open_one", lambda name: got.append(("terminal", name)) or 0)
    monkeypatch.setattr(machine, "open_all", lambda names: got.append(("terminals", names)) or 0)
    monkeypatch.setattr(
        machine,
        "open_vscode",
        lambda rec, status=None: got.append(("vscode", rec["name"], rec["mode"])) or 0,
    )
    for name, mode in (("job1", "colleague"), ("job2", "interactive")):
        core.save(record(name, mode=mode, instance_id=fake_vast.add_instance(1, name)))
        machines.replies[(name, "status")] = machine_status(mode, "tmux" if mode == "colleague" else "vscode")
    return got


def machine_status(mode: str, frontend: str) -> dict:
    """What the machine's status command says."""
    return {"name": "job", "agent": {"state": "working", "message": "on it"}, "idle_seconds": 30, "used_hours": 0.5,
            "mode": mode, "frontend": frontend, "stopping": None, "stop_reason": None, "stop_sent_for": None,
            "git": {"dirty": 0, "unpushed": 0}}  # fmt: skip


def test_an_interactive_agent_in_vs_code_opens_there_and_open_all_leaves_it_out(opened, capsys):
    assert run(capsys, "open", "job2")[0] == 0  # (without a terminal to wait in for its hand-off)
    assert run(capsys, "open", "job2", "--vscode")[0] == 0
    rc, out, _ = run(capsys, "open", "--all")
    assert rc == 0 and "job2" in out
    assert opened == [("vscode", "job2", "interactive")] * 2 + [("terminals", ["job1"])]


def test_after_a_hand_off_an_interactive_agent_opens_in_a_terminal(opened, machines, capsys):
    machines.replies[("job2", "status")] = machine_status("autonomous", "tmux")
    assert run(capsys, "open", "job2")[0] == 0 and run(capsys, "open", "--all")[0] == 0
    assert opened == [("terminal", "job2"), ("terminals", ["job1", "job2"])]


def launching(
    fake_vast, monkeypatch, mode: str = "colleague", end: str = "ready", at_work: bool = True
) -> None:
    """Agent job3, being launched: each look at its lock moves the launch on (a line, a status line, then the
    phase it ends in). Without `at_work`, no launch holds the lock (it was cut off)."""
    core.save(record("job3", phase="renting", mode=mode, instance_id=None))
    launch.append_log("job3", "Renting two machines at once: offer 1 and offer 2")

    def finish() -> None:
        rec = core.load("job3")
        rec.update(phase=end, error="every offer failed (1: taken)" if end == "failed" else None)
        if end == "ready":
            rec["instance_id"] = fake_vast.add_instance(1, "job3")
        core.save(rec)
        launch.append_log(
            "job3", "Ready after 1m30s." if end == "ready" else "Launch failed: every offer failed"
        )

    steps = [lambda: launch.append_log("job3", "~ Booting: loading - Pull complete (0m40s)"), finish]
    monkeypatch.setattr(core, "is_locked", lambda name: bool(at_work and steps) and (steps.pop(0)() or True))


def test_open_follows_a_launch_then_attaches_to_the_agent(opened, fake_vast, monkeypatch, capsys):
    launching(fake_vast, monkeypatch)
    rc, out, _ = run(capsys, "open", "job3")
    assert rc == 0 and opened == [("terminal", "job3")]
    for shown in ("Renting two machines at once", "Pull complete", "Ready after 1m30s"):
        assert shown in out


def test_open_follows_an_interactive_agents_launch_into_vs_code(
    opened, fake_vast, machines, monkeypatch, capsys
):
    launching(fake_vast, monkeypatch, mode="interactive")
    machines.replies[("job3", "status")] = machine_status("interactive", "vscode")
    assert run(capsys, "open", "job3")[0] == 0 and opened == [("vscode", "job3", "interactive")]


@pytest.mark.parametrize("end,at_work", [("failed", True), ("renting", False)])
def test_open_on_a_launch_that_fails_or_was_cut_off_says_what_to_do(end, at_work, opened, fake_vast, monkeypatch,
                                                                     capsys):  # fmt: skip
    launching(fake_vast, monkeypatch, end=end, at_work=at_work)
    rc, out, _ = run(capsys, "open", "job3")
    assert rc == 1 and opened == [] and "crab-student start job3" in out
    assert ("every offer failed" in out) is (end == "failed")


# ---- guide and offers ----------------------------------------------------------------------------


def test_guide_launch_prints_the_guide(capsys):
    rc, out, _ = run(capsys, "guide", "launch")
    assert rc == 0 and out.strip() == setup.guide("launch").strip()
    assert run(capsys, "guide", "other")[0] == 2


def test_offers_lists_what_fits_cheapest_first_and_rents_nothing(fake_vast, capsys):
    fake_vast.offers = [
        offer(1, dph_total=0.90),
        offer(2, dph_total=0.30),
        offer(3, dph_total=0.33, num_gpus=2),
    ]
    data = json.loads(run(capsys, "offers", "--gpus", "1-2", "--json")[1])
    assert [o["id"] for o in data["offers"]] == [2, 3, 1] and data["count"] == 3
    assert data["offers"][0].keys() >= {"usd_per_hour", "gpus", "vcpus", "host_vcpus", "down_usd_per_tb"}
    data = json.loads(run(capsys, "offers", "--gpus", "1", "--limit", "1", "--json")[1])
    assert [o["id"] for o in data["offers"]] == [2] and data["count"] == 2
    rc, out, _ = run(capsys, "offers", "--gpus", "1-2")
    assert rc == 0 and out.isascii() and sum("RTX 4090" in line for line in out.splitlines()) == 3
    assert all(method == "POST" for method, *_ in fake_vast.requests)  # (searches only)


def test_offers_kinds_are_by_gpu_or_by_cpu_for_a_task_with_no_gpu(fake_vast, capsys):
    fake_vast.offers = [
        offer(1, dph_total=0.40),
        offer(2, dph_total=0.50),
        offer(3, dph_total=0.45, gpu_ram=49152),
        cpu_offer(4),
        cpu_offer(5, cpu_name="Xeon E5-2680 v4", cpu_cores_effective=28, cpu_cores=56),
    ]
    kinds = json.loads(run(capsys, "offers", "--gpus", "1", "--kinds", "--json")[1])["kinds"]
    assert [(k["kind"], k["offers"]) for k in kinds] == [
        ("1x RTX 3060 12GB", 2),
        ("1x RTX 4090 24GB", 2),
        ("1x RTX 4090 48GB", 1),
    ]
    kinds = json.loads(run(capsys, "offers", "--kinds", "--json")[1])["kinds"]  # (offers 1-3: 16 vCPUs)
    assert [(k["offers"], k["whole_cpu"]) for k in kinds] == [(1, 1), (1, 0)]
    assert kinds[0]["kind"].startswith("AMD Ryzen") and kinds[1]["kind"] == "Xeon E5-2680 v4"
    assert run(capsys, "offers", "--kinds")[0] == 0


def test_offers_with_nothing_fitting_is_exit_4(fake_vast, capsys):
    fake_vast.offers = [offer(1)]  # (16 vCPUs: under a CPU task's minimum)
    assert run(capsys, "offers")[0] == 4
