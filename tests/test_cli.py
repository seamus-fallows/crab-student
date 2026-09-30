"""The command line: exit codes, stdout and stderr, --json, ASCII, and what each command hands its layer."""

from __future__ import annotations

import dataclasses
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_vast import offer

from crab_student import __version__, agents, cli, core, github, launch, machine, setup

VIEW_KEYS = {f.name for f in dataclasses.fields(agents.View)}
BATCH = """=== alpha --hours 2 --gpus 1 --secret WANDB_KEY --mode interactive --no-fast
# Task A
Train it.
=== beta --model opus --cpus 32
# Task B
Sweep it.
"""


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
            "repo": {"slug": "ada/proj", "branch": f"agent/{name}", "workspace": False, "dir": "/root/work/proj"},
            "offers": [], "dph": 0.42, "short": "1x RTX 4090 (Quebec, CA)", "mode": "colleague",
            "claude_settings": {}, "idle_minutes": 60, "req": {"hours": None}, **fields}  # fmt: skip


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

    for name in ("approve_all", "start"):
        stand_in(launch, name, result())
    stand_in(launch, "decline", "Declined job1: nothing was rented.")
    for name in ("stop", "delete", "send"):
        stand_in(agents, name, {"name": "job1", "result_line": f"{name}: done"})
    stand_in(agents, "report", "# Report\n")
    stand_in(agents, "views", [view()])
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


@pytest.fixture
def batched(launched, monkeypatch) -> list[list[launch.LaunchArgs]]:
    """launch.launch_batch replaced too: the LaunchArgs of each batch."""
    got: list[list[launch.LaunchArgs]] = []
    monkeypatch.setattr(launch, "launch_batch", lambda items, out: got.append(items) or result())
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
    ["", "launch", "frobnicate", "extend job1 2", "open", "open job1 --all", "launch job1 --hours 0",
     "launch job1 --hours 1 --repo . --no-repo", "launch job1 --hours nan", "offers --hours inf",
     "offers --count 0", "launch job1 --hours 1 --brief-file no-such-brief.md", "launch job1 --mode solo",
     "launch job1 --effort max", "launch job1 --fast on", "launch --hours 1", "launch job1 --batch --hours 1",
     "approve"],
)  # fmt: skip
def test_usage_mistakes_exit_2(argv, launched, batched, capsys):
    assert exit_code(argv.split()) == 2 and not launched and not batched


def test_version(capsys):
    assert exit_code(["--version"]) == 0 and __version__ in capsys.readouterr().out


def test_launch_json_is_one_value_with_progress_on_stderr(monkeypatch, capsys):
    def fake_launch(a: launch.LaunchArgs, out: launch.Progress) -> dict:
        out.line("Planning job1 on a machine in Montréal")
        out.status("Booting")
        return {**result(), "machine": "Montréal"}

    monkeypatch.setattr(launch, "launch", fake_launch)
    monkeypatch.setattr("sys.stdin", io.StringIO("# Task\n"))
    rc, out, err = run(capsys, "launch", "job1", "--hours", "1", "--json")
    assert rc == 0 and len(out.splitlines()) == 1 and json.loads(out)["result_line"]
    assert "Planning job1" in err and "Booting" in err and out.isascii() and err.isascii()


@pytest.mark.parametrize(
    "argv",
    ["approve job1", "approve job1 job2", "start job1", "stop job1", "delete job1", "list",
     "status job1", "config", "config regions", "config regions CA", "config regions --reset", "secret list",
     "secret delete HF_TOKEN"],
)  # fmt: skip
def test_json_prints_exactly_one_value(argv, patched, capsys):
    rc, out, _ = run(capsys, *argv.split(), "--json")
    json.loads(out)
    assert rc == 0 and len(out.splitlines()) == 1


@pytest.mark.parametrize(
    "argv, layer, args",
    [("approve job1", "approve_all", (["job1"],)), ("approve job1 job2", "approve_all", (["job1", "job2"],)),
     ("decline job1", "decline", ("job1",)), ("start job1", "start", ("job1",)), ("stop job1", "stop", ("job1",)),
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
    rc, out, _ = run(capsys, "launch", "job1", "--hours", "1", "--json")
    assert rc == 3 and json.loads(out)["needs_approval"]


def test_all_output_is_ascii_but_the_agents_report(patched, monkeypatch, capsys):
    monkeypatch.setattr(agents, "views", lambda: [view(message="naïve ✓")])
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
    assert cli.main(["launch", "job1", "--hours", "2"]) == 0
    brief = tmp_path / "brief.md"
    brief.write_text("# Task\nFrom a file, café.\n", encoding="utf-8")
    assert cli.main(["launch", "job2", "--hours", "2", "--brief-file", str(brief)]) == 0
    # Windows decodes a pipe with its ANSI code page unless told otherwise; Git Bash sends UTF-8.
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO("Café ✓\n".encode()), encoding="cp1252"))
    assert cli.main(["launch", "job3", "--hours", "2"]) == 0
    briefs = [a.brief for a in launched]
    assert briefs == ["# Task\nDo the thing.\n", "# Task\nFrom a file, café.\n", "Café ✓\n"]


def test_launch_refuses_a_missing_brief(ready, monkeypatch, capsys):
    core.write_private(core.vast_key_file(), "key\n")
    for stdin in (SimpleNamespace(isatty=lambda: True), io.StringIO(""), io.StringIO(" \n")):
        monkeypatch.setattr("sys.stdin", stdin)
        rc, _, err = run(capsys, "launch", "job1", "--hours", "1")
        assert rc == 2 and err.startswith("error:")
    assert not core.record_path("job1").exists()


@pytest.mark.parametrize(
    "flags, gpus", [("", 0), ("--gpu-ram 24", 1), ("--gpu-models 4090,A100", 1), ("--gpus 2 --gpu-ram 40", 2)]
)
def test_asking_for_gpu_memory_or_models_means_a_gpu(flags, gpus, launched, capsys):
    assert cli.main(["launch", "job1", "--hours", "3", *flags.split()]) == 0
    assert launched[0].req.gpus == gpus


def test_launch_passes_its_options_on(launched, tmp_path, capsys):
    core.set_config("disk_gb", "80")
    argv = ("launch job1 --hours 3 --gpu-ram 24 --secret HF_TOKEN --secret WANDB_KEY --model sonnet --mode autonomous "
            "--effort xhigh --fast")  # fmt: skip
    assert cli.main([*argv.split(), "--repo", str(tmp_path), "--dry-run", "--fake-claude"]) == 0
    a = launched[0]
    assert (a.name, a.req.hours, a.req.gpu_ram_gb, a.req.disk_gb) == ("job1", 3, 24, 80)
    assert a.secrets == ["HF_TOKEN", "WANDB_KEY"] and a.repo_path == tmp_path.resolve() and not a.no_repo
    assert (a.model, a.mode, a.effort, a.fast, a.dry_run, a.fake_claude) == (
        "sonnet", "autonomous", "xhigh", True, True, True)  # fmt: skip


def test_launch_without_options_leaves_hours_and_the_agent_settings_open(launched, capsys):
    assert cli.main(["launch", "job1"]) == 0 and cli.main(["launch", "job2", "--no-fast"]) == 0
    a, b = launched
    assert (a.req.hours, a.req.cpus, a.mode, a.model, a.effort, a.fast) == (None, 8, None, None, None, None)
    assert b.fast is False


# ---- launch --batch ------------------------------------------------------------------------------


def test_a_batch_gives_each_agent_its_brief_its_own_flags_and_the_batch_flags_it_does_not_set(
    batched, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr("sys.stdin", io.StringIO(BATCH))
    argv = ["launch", "--batch", "--hours", "3", "--cpus", "12", "--secret", "HF_TOKEN", "--model", "sonnet",
            "--effort", "high", "--fast", "--repo", str(tmp_path), "--dry-run"]  # fmt: skip
    assert cli.main(argv) == 0
    [(alpha, beta)] = batched
    assert (alpha.name, alpha.brief.strip()) == ("alpha", "# Task A\nTrain it.")
    assert (beta.name, beta.brief.strip()) == ("beta", "# Task B\nSweep it.")
    assert (alpha.req.hours, alpha.req.gpus, alpha.req.cpus, alpha.model) == (2, 1, 12, "sonnet")
    assert (beta.req.hours, beta.req.gpus, beta.req.cpus, beta.model) == (3, 0, 32, "opus")
    assert (alpha.mode, alpha.effort, alpha.fast) == ("interactive", "high", False)
    assert (beta.mode, beta.effort, beta.fast) == (None, "high", True)
    assert sorted(alpha.secrets) == ["HF_TOKEN", "WANDB_KEY"] and beta.secrets == ["HF_TOKEN"]  # (added up)
    assert all(a.dry_run and a.repo_path == tmp_path.resolve() for a in (alpha, beta))
    brief = tmp_path / "batch.md"
    brief.write_text(BATCH, encoding="utf-8")
    assert cli.main(["launch", "--batch", "--hours", "1", "--brief-file", str(brief)]) == 0
    assert [a.name for a in batched[1]] == ["alpha", "beta"]


@pytest.mark.parametrize(
    "text",
    ["# Tasks\n" + BATCH, "one brief, no === line\n", "", "=== alpha --hours soon\nbrief\n",
     "=== alpha --frobnicate\nbrief\n", "=== alpha and more\nbrief\n", "=== \nbrief\n",
     "=== alpha 'unclosed\nbrief\n", "=== alpha --mode solo\nbrief\n"],
)  # fmt: skip
def test_a_malformed_batch_exits_2_before_anything_is_launched(text, batched, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    rc, _, err = run(capsys, "launch", "--batch", "--hours", "1")
    assert rc == 2 and err.startswith("error:") and not batched


def test_hours_are_optional_in_a_batch_too(batched, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(BATCH))  # (beta has no --hours of its own)
    assert run(capsys, "launch", "--batch")[0] == 0
    assert [a.req.hours for a in batched[0]] == [2, None]


# ---- list and status -----------------------------------------------------------------------------


def test_list_fits_the_terminal_and_keeps_its_columns_apart(monkeypatch, capsys):
    long = ", ".join(["epoch 3/10 val loss 0.41"] * 6)
    views = [view("ablation-1", message=long), view("big-train", state="failed", used_h=None, spent=None),
             view("sweep", state="done", used_h=100.5, spent=150.25),
             view("resnet-rerun", state="interactive", mode="interactive", message="in VS Code, idle 12m")]  # fmt: skip
    monkeypatch.setattr(agents, "views", lambda: views)
    for width in (80, 120, 250):
        monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda *_, w=width: os.terminal_size((w, 24)))
        lines = run(capsys, "list")[1].splitlines()
        assert len(lines) == 1 + len(views) and all(len(line) < width for line in lines), width
        for column in ("MODE", "STATE", "MESSAGE", "TIME", "SPENT"):
            at = lines[0].index(column)
            assert all(line[at - 1] == " " for line in lines), (width, column)
        assert (long in lines[1]) == (width == 250)


def test_list_without_agents_and_as_json(monkeypatch, capsys):
    monkeypatch.setattr(agents, "views", lambda: [])
    assert "No agents yet" in run(capsys, "list")[1]
    stopped = view("job2", state="stopped", stopped_h=26.5, disk_usd_day=0.17)
    monkeypatch.setattr(agents, "views", lambda: [view(), stopped])
    rows = json.loads(run(capsys, "list", "--json")[1])
    assert [r["name"] for r in rows] == ["job1", "job2"] and all(set(r) == VIEW_KEYS for r in rows)
    assert (rows[1]["stopped_h"], rows[1]["disk_usd_day"]) == (26.5, 0.17)


def test_status_detail_and_a_json_result_line_that_follows_the_state(patched, monkeypatch, capsys):
    rc, out, _ = run(capsys, "status", "job1")
    assert rc == 0 and "job1" in out.splitlines()[0] and "working" in out.splitlines()[0]
    for part in (view().machine, core.usd(view().spent), "agent/job1", "crab-student open job1",
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
        machine, "open_vscode", lambda rec: got.append(("vscode", rec["name"], rec["mode"])) or 0
    )
    for name, mode in (("job1", "colleague"), ("job2", "interactive")):
        core.save(record(name, mode=mode, instance_id=fake_vast.add_instance(1, name)))
    machines.replies[("job2", "status")] = {"mode": "interactive", "frontend": "vscode"}
    return got


def test_an_interactive_agent_in_vs_code_opens_there_and_open_all_leaves_it_out(opened, machines, capsys):
    assert run(capsys, "open", "job2")[0] == 2
    assert run(capsys, "open", "job2", "--vscode")[0] == 0
    rc, out, _ = run(capsys, "open", "--all")
    assert rc == 0 and "job2" in out
    assert opened == [("vscode", "job2", "interactive"), ("terminals", ["job1"])]
    assert {call[0] for call in machines.calls} == {"job2"}  # (only an interactive agent is asked)


def test_open_follows_a_hand_off_to_tmux(opened, machines, capsys):
    machines.replies[("job2", "status")] = {"mode": "autonomous", "frontend": "tmux"}
    assert run(capsys, "open", "job2")[0] == 0
    assert opened == [("terminal", "job2")] and core.load("job2")["mode"] == "autonomous"
    assert run(capsys, "open", "job2", "--vscode")[0] == 0 and opened[-1] == ("vscode", "job2", "autonomous")


# ---- guide and offers ----------------------------------------------------------------------------


def test_guide_launch_prints_the_guide(capsys):
    rc, out, _ = run(capsys, "guide", "launch")
    assert rc == 0 and out.strip() == setup.guide("launch").strip()
    assert run(capsys, "guide", "other")[0] == 2


def test_offers_shows_the_pick_and_rents_nothing(fake_vast, capsys):
    fake_vast.offers = [offer(1, dph_total=0.30), offer(2, dph_total=0.33), offer(3, dph_total=0.90)]
    data = json.loads(run(capsys, "offers", "--gpus", "1", "--json")[1])
    assert data["pick"] == data["offers"][0]["id"] == 1 and len(data["offers"]) == 3
    rc, out, _ = run(capsys, "offers", "--gpus", "1")
    assert rc == 0 and len([line for line in out.splitlines() if "RTX 4090" in line]) == 3
    assert all(method == "POST" for method, *_ in fake_vast.requests)  # (searches only)
