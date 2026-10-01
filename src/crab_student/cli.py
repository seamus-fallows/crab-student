"""The `crab-student` command: the parser, the handlers and all terminal output."""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import re
import shlex
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from crab_student import __version__, agents, core, github, launch, machine, offers, setup, vast


def say(text: str = "", err: bool = False) -> None:
    print(core.ascii_text(text), file=sys.stderr if err else sys.stdout, flush=True)


def emit(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result))
    else:
        say(result["result_line"])


# ---- requirements --------------------------------------------------------------------------------

HOURS_HELP = "how long the task may run, if more than a day: its host must stay available twice as long"


def _req_args(p: argparse.ArgumentParser, defaults: bool = True) -> None:
    """The requirement flags (without their defaults for a batch's agent, so the batch's own apply)."""

    def d(value: object) -> dict:
        return {"default": value} if defaults else {}

    p.add_argument("--gpus", type=int, **d(0), help="number of GPUs (default 0: a CPU task)")
    p.add_argument("--gpu-ram", type=float, **d(0), metavar="GB", help="memory per GPU")
    p.add_argument("--gpu-models", **d(""), metavar="LIST", help='allowed GPU models, e.g. "4090,A100"')
    p.add_argument("--cpus", type=int, **d(8), help="CPU cores (default 8)")
    p.add_argument("--ram", type=float, **d(16), metavar="GB", help="system RAM (default 16)")
    p.add_argument("--disk", type=float, metavar="GB", help="disk (default: the disk_gb setting)")
    p.add_argument(
        "--min-down", type=float, metavar="MBPS", help="minimum download speed (default: the setting)"
    )


def _req(args: argparse.Namespace, cfg: dict) -> offers.Req:
    models = [m.strip() for m in args.gpu_models.split(",") if m.strip()]
    gpus = args.gpus or (1 if args.gpu_ram or models else 0)
    hours = args.hours
    numbers = (hours or 1, args.gpu_ram, args.ram, args.disk or 1, args.min_down or 0)
    if (
        not all(math.isfinite(x) for x in numbers)
        or (hours is not None and hours <= 0)
        or gpus < 0
        or args.cpus < 1
    ):
        raise core.CrabError("hours must be a number above 0, and the other requirements plain numbers.", 2)
    return offers.Req(
        hours=hours,
        gpus=gpus,
        gpu_ram_gb=args.gpu_ram,
        gpu_models=models,
        cpus=args.cpus,
        ram_gb=args.ram,
        disk_gb=args.disk or cfg["disk_gb"],
        min_down_mbps=cfg["min_download_mbps"] if args.min_down is None else args.min_down,
    )


# ---- commands ------------------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    return setup.init(renew_claude_token=args.claude_token)


def cmd_config(args: argparse.Namespace) -> int:
    cfg = core.load_config()
    if args.key and (args.reset or args.value is not None):
        value = core.set_config(args.key, None if args.reset else args.value)
        say(json.dumps(value) if args.json else f"{args.key} = {_show(value)}")
    elif args.key:
        if args.key not in core.DEFAULTS:
            core.parse_value(args.key, "")  # (raises: unknown setting)
        say(json.dumps(cfg[args.key]) if args.json else _show(cfg[args.key]))
    elif args.json:
        print(json.dumps(cfg))
    else:
        width = max(map(len, cfg)) + 2
        for key, value in cfg.items():
            say(f"{key:<{width}}{_show(value)}{'   (default)' if value == core.DEFAULTS[key] else ''}")
    return 0


def _show(value: object) -> str:
    return ", ".join(value) if isinstance(value, list) else str(value)


def cmd_offers(args: argparse.Namespace) -> int:
    cfg = core.load_config()
    cap = cfg["approve_above_usd_per_hour"]
    plan = offers.plan(_req(args, cfg), cfg, cap, mode=args.mode or cfg["mode"])
    shown, backups = plan.ranked[: args.count], {o["id"] for o in plan.fallbacks}
    if args.json:
        print(json.dumps({"offers": shown, "pick": plan.primary["id"], "fallbacks": sorted(backups)}))
        return 0
    width = max(len(offers.summary(o)) for o in shown)
    say(f"   {'OFFER':<10}{'MACHINE':<{width + 2}}{'$/HR':<8}{'DOWN':<11}RELIABILITY")
    for o in shown:
        mark = "*" if o is plan.primary else "+" if o["id"] in backups else " "
        rel = o.get("reliability2") or o.get("reliability") or 0
        say(
            f"{mark}  {o['id']:<10}{offers.summary(o):<{width + 2}}${o['dph_total']:<7.2f}"
            f"{o.get('inet_down', 0):>5.0f} Mbps  {rel * 100:.1f}%"
        )
    say(
        f"\n{len(plan.ranked)} machines fit {plan.req.describe()}. * the pick, + its backups. Nothing was rented."
    )
    if plan.over_cap:
        say(
            f"Every machine that fits costs over ${cap:.2f}/hr, so a launch would ask for your approval first."
        )
    return 0


def _positive(text: str) -> int:
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError("must be a whole number, 1 or more")
    return int(text)


def cmd_launch(args: argparse.Namespace) -> int:
    if args.repo and args.no_repo:
        raise core.CrabError("use either --repo or --no-repo, not both.", 2)
    if bool(args.name) == args.batch:
        raise core.CrabError("give the agent's NAME, or --batch for several agents.", 2)
    text = _read_brief(args)
    if args.batch:
        res = launch.launch_batch(_batch(args, text), launch.Progress())
    else:
        res = launch.launch(_launch_args(args, args.name, text), launch.Progress())
    emit(res, args.json)
    return res["exit_code"]


def _read_brief(args: argparse.Namespace) -> str:
    if args.brief_file:
        try:
            return Path(args.brief_file).read_text(encoding="utf-8")
        except OSError as e:
            raise core.CrabError(f"could not read {args.brief_file}: {e}", 2) from None
    if sys.stdin.isatty():
        raise core.CrabError("give the task brief on stdin or with --brief-file.", 2)
    return sys.stdin.read()


def _launch_args(args: argparse.Namespace, name: str, brief: str) -> launch.LaunchArgs:
    return launch.LaunchArgs(
        name=name,
        req=_req(args, core.load_config()),
        brief=brief,
        repo_path=Path(args.repo).resolve() if args.repo else None,
        no_repo=args.no_repo,
        secrets=args.secret or [],
        fake_claude=args.fake_claude,
        dry_run=args.dry_run,
        mode=args.mode,
        model=args.model,
        effort=args.effort,
        fast=args.fast,
    )


def _agent_args(p: argparse.ArgumentParser) -> None:
    """The agent's own flags, on launch and on a batch's === lines."""
    p.add_argument("--secret", action="append", metavar="NAME", help="a stored secret to give the agent")
    p.add_argument(
        "--mode",
        choices=core.MODES,
        help="autonomous: never asks, decides and records why; colleague: asks only about important "
        "choices; interactive: you work with it in VS Code (default: the mode setting)",
    )
    p.add_argument("--model", help="the agent's Claude model (default: the model setting, else your own)")
    p.add_argument(
        "--effort", choices=core.EFFORTS, help="its effort level (default: the setting, else your own)"
    )
    p.add_argument(
        "--fast",
        action=argparse.BooleanOptionalAction,
        help="fast mode on or off (default: the setting, else your own)",
    )


class _AgentParser(argparse.ArgumentParser):
    def error(self, message: str):
        raise core.CrabError(f"a '===' line: {message}", 2)


def _batch(args: argparse.Namespace, text: str) -> list[launch.LaunchArgs]:
    """Several agents from one text: each starts with a line `=== NAME [flags]` (flags as for launch, over
    the batch's own), followed by its brief."""
    parts = re.split(r"(?m)^=== ", text)
    if parts[0].strip() or len(parts) < 2:
        raise core.CrabError("a batch is one or more agents, each starting with a line: === NAME [flags]", 2)
    own = _AgentParser(prog="===", add_help=False, argument_default=argparse.SUPPRESS)
    own.add_argument("name")
    own.add_argument("--hours", type=float)
    _req_args(own, defaults=False)
    _agent_args(own)
    items = []
    for part in parts[1:]:
        head, _, brief = part.partition("\n")
        try:
            agent = vars(own.parse_args(shlex.split(head)))
        except ValueError as e:
            raise core.CrabError(f"a '===' line: {e}", 2) from None
        secrets = list(dict.fromkeys((args.secret or []) + agent.get("secret", [])))
        merged = argparse.Namespace(**{**vars(args), **agent, "secret": secrets})
        items.append(_launch_args(merged, agent["name"], brief))
    return items


def cmd_approve(args: argparse.Namespace) -> int:
    res = launch.approve_all(args.name, launch.Progress())
    emit(res, args.json)
    return res["exit_code"]


def cmd_decline(args: argparse.Namespace) -> int:
    say(launch.decline(args.name))
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    res = launch.start(args.name, launch.Progress())
    emit(res, args.json)
    return res["exit_code"]


def cmd_stop(args: argparse.Namespace) -> int:
    emit(agents.stop(args.name, launch.Progress()), args.json)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    emit(agents.delete(args.name, args.force, launch.Progress()), args.json)
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    text = " ".join(args.text) if args.text else ("" if sys.stdin.isatty() else sys.stdin.read())
    say(agents.send(args.name, text)["result_line"])
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    text = agents.report(args.name) + "\n"  # (the agent's words, in whatever characters the console has)
    sys.stdout.buffer.write(text.encode(sys.stdout.encoding or "utf-8", "replace"))
    sys.stdout.flush()
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    say(github.pull(core.load(args.name), Path(args.repo) if args.repo else None))
    return 0


def cmd_guide(args: argparse.Namespace) -> int:
    say(setup.guide(args.topic))
    return 0


def cmd_secret(args: argparse.Namespace) -> int:
    names = core.secret_names()
    if args.action == "list":
        print(json.dumps(names)) if args.json else say("\n".join(names) or "No secrets stored.")
        return 0
    if not args.name or not core.SECRET_NAME_RE.fullmatch(args.name):
        raise core.CrabError(
            "give the secret's NAME: capitals, digits and _, e.g. HF_TOKEN (it becomes an "
            "environment variable on the machine).",
            2,
        )
    if args.action == "delete":
        core.delete_secret(core.secret_key(args.name))
        core.set_secret_names([n for n in names if n != args.name])
        emit({"name": args.name, "result_line": f"Deleted {args.name}."}, args.json)
        return 0
    if not setup.interactive():
        raise core.CrabError(f"secret set asks for the value, but {setup.NOT_A_TERMINAL}", 2)
    value = getpass.getpass(f"Value of {args.name} (it will not be shown): ").strip()
    if not value:
        raise core.CrabError("the value is empty; nothing was stored.", 2)
    core.set_secret(core.secret_key(args.name), value)
    core.set_secret_names([*names, args.name])
    line = f"Stored {args.name}. Attach it to a launch with --secret {args.name}."
    emit({"name": args.name, "result_line": line}, args.json)
    return 0


# ---- list, status, open --------------------------------------------------------------------------


def table(views: list[agents.View]) -> list[str]:
    if not views:
        return [
            "No agents yet. Ask Claude Code to run something with crab-student, or see crab-student launch --help."
        ]
    name_w = max(4, *(len(v.name) for v in views)) + 2
    # MODE fits "interactive", STATE "interrupted" and TIME "999h59m" with a gap; the last column stays free (a
    # line that fills the terminal wraps on some consoles).
    rest = name_w + 12 + 13 + 2 + 8 + 10 + 1
    msg_w = max(20, shutil.get_terminal_size((110, 20)).columns - rest)
    lines = [f"{'NAME':<{name_w}}{'MODE':<12}{'STATE':<13}{'MESSAGE':<{msg_w + 2}}{'TIME':<8}SPENT"]
    for v in views:
        msg = v.message if len(v.message) <= msg_w else v.message[: msg_w - 3] + "..."
        spent = "-" if v.spent is None else "~" + core.usd(v.spent)
        lines.append(
            f"{v.name:<{name_w}}{v.mode:<12}{v.state:<13}{msg:<{msg_w + 2}}{core.fmt_hours(v.used_h):<8}{spent}"
        )
    return lines


def cmd_list(args: argparse.Namespace) -> int:
    views = agents.views()
    if args.json:
        print(json.dumps([asdict(v) for v in views]))
    else:
        say("\n".join(table(views)))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    v = agents.view(args.name)
    rec = core.load(args.name)
    headline = f"{v.name}: {v.state} - {v.message}"
    if args.json:
        # The launch's line says where it works and how to watch or open it, or what to approve; any other
        # state gets the headline.
        line = headline
        if v.state in ("working", "interactive", "approval"):
            line = launch.result(rec, v.state, [], (rec.get("pending") or {}).get("reason"))["result_line"]
        print(json.dumps({**asdict(v), "result_line": line}))
        return 0
    say(headline)
    if v.machine:
        say(
            f"  machine   {v.machine} - ${v.dph:.2f}/hr (instance {rec['instance_id']}, {v.vast_status or 'unknown'})"
        )
    where = {"tmux": ", in tmux", "vscode": ", in VS Code"}.get(v.frontend or "", "")
    say(f"  agent     {v.mode}{where} - Claude Code: {launch.settings_text(v.settings)}")
    git = v.git or {}
    pushed = (
        ""
        if not git
        else " (all pushed)"
        if not (git.get("dirty") or git.get("unpushed"))
        else f" ({git.get('dirty', 0)} uncommitted, {git.get('unpushed', 0)} unpushed)"
    )
    say(f"  repo      {v.repo}, branch {v.branch}{pushed}")
    if rec["phase"] == "ready":
        idle = f"; idle {v.idle_min} min" if v.idle_min is not None else ""
        say(
            f"  time      {core.fmt_hours(v.used_h)} of machine time{idle}; it stops after "
            f"{rec['idle_minutes']} min without activity"
        )
        say(f"  money     ~{core.usd(v.spent)} so far at ${v.dph:.2f}/hr" if v.dph else "  money     -")
    if v.stopped_h is not None:
        disk = (
            f"; its disk costs about ${v.disk_usd_day:.2f}/day until you delete it" if v.disk_usd_day else ""
        )
        say(f"  stopped   {core.fmt_hours(v.stopped_h)} ago{disk}")
    if rec["phase"] == "ready":
        if v.frontend == "vscode" or (v.frontend is None and v.mode == "interactive"):
            say(
                f"  open      crab-student open {v.name} --vscode    (/handoff in its chat: it carries on alone)"
            )
        else:
            say(f"  open      crab-student open {v.name}    (VS Code: --vscode)")
        say(f"  results   crab-student report {v.name} | crab-student pull {v.name}")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    if args.all == bool(args.name):
        raise core.CrabError("give an agent's NAME, or --all.", 2)
    followed = bool(args.name) and core.load(args.name)["phase"] != "ready"
    if followed and not agents.follow_launch(args.name):  # (a launch at work: its progress until it is ready)
        return 1
    recs = [core.load(args.name)] if args.name else [r for r in core.all_records() if r["phase"] == "ready"]
    insts = {i["id"]: i for i in vast.instances()} if recs else {}
    live = [r for r in recs if vast.running(insts.get(r["instance_id"]))]
    for r in live:  # (the port can change after a restart)
        with core.locked(r["name"], wait=30):
            fresh = core.load(r["name"])
            if machine.set_address(fresh, insts[r["instance_id"]]):
                core.save(fresh)
    machine.write_ssh_config()
    if args.name and not live:
        raise core.CrabError(f"{args.name} is not running. Start it first: crab-student start {args.name}")
    if not live:
        raise core.CrabError("no agent is running.")
    if args.vscode or (followed and live[0]["mode"] == "interactive"):
        for r in live:
            machine.open_vscode(r)
        return 0
    return agents.open_one(args.name) if args.name else agents.open_all([r["name"] for r in live])


# ---- the parser ----------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crab-student", description="Claude Code agents on rented Vast.ai machines."
    )
    p.add_argument("--version", action="version", version=f"crab-student {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def cmd(name: str, func, help: str, json_flag: bool = False) -> argparse.ArgumentParser:
        c = sub.add_parser(name, help=help, description=help)
        c.set_defaults(func=func)
        if json_flag:
            c.add_argument("--json", action="store_true", help="print the result as JSON")
        return c

    c = cmd("init", cmd_init, "set up crab-student (safe to run again: it only asks for what is missing)")
    c.add_argument("--claude-token", action="store_true", help="replace the stored Claude token")
    c = cmd("config", cmd_config, "show or change settings", json_flag=True)
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")
    c.add_argument("--reset", action="store_true", help="return KEY to its default")
    c = cmd(
        "offers", cmd_offers, "show the machines a launch would pick from (rents nothing)", json_flag=True
    )
    _req_args(c)
    c.add_argument("--hours", type=float, help=HOURS_HELP)
    c.add_argument(
        "--mode",
        choices=core.MODES,
        help="the agent's mode; interactive prefers hosts near the home setting (default: the mode setting)",
    )
    c.add_argument("--count", type=_positive, default=10, help="how many to show (default 10)")
    c = cmd(
        "launch", cmd_launch, "start an agent on a rented machine (the task brief on stdin)", json_flag=True
    )
    c.add_argument("name", nargs="?", help="3-40 characters: a-z, 0-9 and -")
    c.add_argument("--hours", type=float, help=HOURS_HELP)
    _req_args(c)
    c.add_argument(
        "--batch", action="store_true", help="several agents: each starts with a line === NAME [flags]"
    )
    c.add_argument("--brief-file", metavar="PATH", help="the task brief or batch (else read from stdin)")
    c.add_argument("--repo", metavar="PATH", help="your repo (default: the current folder)")
    c.add_argument("--no-repo", action="store_true", help="work in your private workspace repo instead")
    _agent_args(c)
    c.add_argument("--fake-claude", action="store_true", help="run a stand-in for Claude Code (tests)")
    c.add_argument("--dry-run", action="store_true", help="plan only: rent nothing")
    cmd("approve", cmd_approve, "launch agents waiting for your approval", json_flag=True).add_argument(
        "name", nargs="+"
    )
    c = cmd("start", cmd_start, "start a stopped agent, or continue an interrupted launch", json_flag=True)
    c.add_argument("name")
    cmd("decline", cmd_decline, "drop a launch waiting for your approval").add_argument("name")
    cmd("list", cmd_list, "every agent at a glance", json_flag=True)
    cmd("status", cmd_status, "one agent in detail", json_flag=True).add_argument("name")
    c = cmd(
        "open",
        cmd_open,
        "attach to an agent's Claude Code in this terminal (detach: Ctrl-b d); while it is launching, follow the "
        "launch first",
    )
    c.add_argument("name", nargs="?")
    c.add_argument("--all", action="store_true", help="every running agent side by side")
    c.add_argument(
        "--vscode",
        action="store_true",
        help="open the machine in VS Code instead (an interactive agent: with its task in the Claude panel)",
    )
    c = cmd("send", cmd_send, "type a message to an agent (the text, or stdin)")
    c.add_argument("name")
    c.add_argument("text", nargs="*")
    cmd("report", cmd_report, "show an agent's REPORT.md").add_argument("name")
    c = cmd("pull", cmd_pull, "fetch an agent's branch into your repo (your checkout is not touched)")
    c.add_argument("name")
    c.add_argument("--repo", metavar="PATH")
    cmd("stop", cmd_stop, "save the agent's work, then stop its machine", json_flag=True).add_argument("name")
    c = cmd(
        "delete",
        cmd_delete,
        "destroy an agent's machine and forget it (checks for unsaved work)",
        json_flag=True,
    )
    c.add_argument("name")
    c.add_argument("--force", action="store_true", help="delete even if work would be lost")
    c = cmd("secret", cmd_secret, "task secrets (e.g. HF_TOKEN) kept in your system keyring", json_flag=True)
    c.add_argument("action", choices=("set", "list", "delete"))
    c.add_argument("name", nargs="?")
    cmd("guide", cmd_guide, "print a guide (the launcher reads: guide launch)").add_argument("topic")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Briefs and messages come as UTF-8 (Git Bash too); Windows would decode a pipe with its ANSI code page.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        return args.func(args) or 0
    except core.CrabError as e:
        say(f"error: {e.message}", err=True)
        return e.code
    except KeyboardInterrupt:
        say("interrupted", err=True)
        return 1
    except Exception as e:
        if os.environ.get("CRAB_STUDENT_DEBUG"):
            raise
        say(f"error: unexpected {type(e).__name__}: {e} (please report this)", err=True)
        return 1
