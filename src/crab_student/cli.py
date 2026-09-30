"""The `crab-student` command: the parser, the handlers and all terminal output."""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path

from crab_student import __version__, core, github, launch, machine, offers, setup, vast, workers

ALERTS = {
    "needs you": "needs you",
    "done": "is done",
    "done*": "is done (not all pushed)",
    "failed": "failed",
    "stopped": "stopped",
    "launch failed": "failed to launch",
    "gone": "is gone",
}


def say(text: str = "", err: bool = False) -> None:
    print(core.ascii_text(text), file=sys.stderr if err else sys.stdout, flush=True)


def emit(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result))
    else:
        say(result["result_line"])


# ---- requirements --------------------------------------------------------------------------------


def _req_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--gpus", type=int, default=0, help="number of GPUs (default 0: a CPU task)")
    p.add_argument("--gpu-ram", type=float, default=0, metavar="GB", help="memory per GPU")
    p.add_argument("--gpu-models", default="", metavar="LIST", help='allowed GPU models, e.g. "4090,A100"')
    p.add_argument("--cpus", type=int, default=4, help="CPU cores (default 4)")
    p.add_argument("--ram", type=float, default=16, metavar="GB", help="system RAM (default 16)")
    p.add_argument("--disk", type=float, metavar="GB", help="disk (default: the disk_gb setting)")
    p.add_argument(
        "--min-down", type=float, metavar="MBPS", help="minimum download speed (default: the setting)"
    )


def _req(args: argparse.Namespace, cfg: dict, hours: float) -> offers.Req:
    models = [m.strip() for m in args.gpu_models.split(",") if m.strip()]
    gpus = args.gpus or (1 if args.gpu_ram or models else 0)
    numbers = (hours, args.gpu_ram, args.ram, args.disk or 1, args.min_down or 0)
    if not all(math.isfinite(x) for x in numbers) or hours <= 0 or gpus < 0 or args.cpus < 1:
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
        for key, value in cfg.items():
            say(f"{key:<21} {_show(value)}{'   (default)' if value == core.DEFAULTS[key] else ''}")
    return 0


def _show(value: object) -> str:
    return ", ".join(value) if isinstance(value, list) else str(value)


def cmd_offers(args: argparse.Namespace) -> int:
    cfg = core.load_config()
    plan = offers.plan(_req(args, cfg, args.hours), cfg, _money(args.budget))
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
    say(f"\n{len(plan.ranked)} machines fit {plan.req.describe()}. * the pick, + its backups.")
    say(
        f"A {plan.req.hours:g}h launch would get a budget of {core.usd(plan.budget_usd)} (it stops after "
        f"{core.fmt_hours(plan.budget_usd / plan.primary['dph_total'])}). Nothing was rented."
    )
    return 0


def _positive(text: str) -> int:
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError("must be a whole number, 1 or more")
    return int(text)


def _money(value: float | None) -> float | None:
    if value is not None and not (math.isfinite(value) and value > 0):
        raise core.CrabError("a budget must be a number of dollars above 0.", 2)
    return value


def cmd_launch(args: argparse.Namespace) -> int:
    cfg = core.load_config()
    if args.brief_file:
        try:
            brief = Path(args.brief_file).read_text(encoding="utf-8")
        except OSError as e:
            raise core.CrabError(f"could not read the brief file: {e}", 2) from None
    elif sys.stdin.isatty():
        raise core.CrabError("give the task brief on stdin or with --brief-file.", 2)
    else:
        brief = sys.stdin.read()
    if args.repo and args.no_repo:
        raise core.CrabError("use either --repo or --no-repo, not both.", 2)
    a = launch.LaunchArgs(
        name=args.name,
        req=_req(args, cfg, args.hours),
        budget=_money(args.budget),
        brief=brief,
        repo_path=Path(args.repo).resolve() if args.repo else None,
        no_repo=args.no_repo,
        secrets=args.secret or [],
        model=args.model,
        fake_claude=args.fake_claude,
        dry_run=args.dry_run,
    )
    res = launch.launch(a, launch.Progress())
    emit(res, args.json)
    return res["exit_code"]


def cmd_approve(args: argparse.Namespace) -> int:
    res = launch.approve(args.name, launch.Progress())
    emit(res, args.json)
    return res["exit_code"]


def cmd_decline(args: argparse.Namespace) -> int:
    say(launch.decline(args.name))
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    res = launch.start(args.name, launch.Progress())
    emit(res, args.json)
    return res["exit_code"]


def cmd_extend(args: argparse.Namespace) -> int:
    res = launch.extend(args.name, args.hours)
    emit(res, args.json)
    return res["exit_code"]


def cmd_stop(args: argparse.Namespace) -> int:
    emit(workers.stop(args.name, launch.Progress()), args.json)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    emit(workers.delete(args.name, args.force, launch.Progress()), args.json)
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    text = " ".join(args.text) if args.text else ("" if sys.stdin.isatty() else sys.stdin.read())
    say(workers.send(args.name, text)["result_line"])
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    text = workers.report(args.name) + "\n"  # (the worker's words, in whatever characters the console has)
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


def table(views: list[workers.View]) -> list[str]:
    if not views:
        return [
            "No workers yet. Ask Claude Code to run something with crab-student, or see crab-student launch --help."
        ]
    money = [f"{'-' if v.spent is None else '~' + core.usd(v.spent)} / {core.usd(v.budget)}" for v in views]
    name_w = max(4, *(len(v.name) for v in views)) + 2
    # STATE fits "launch failed" and TIME "999h59m" with a gap; the last column stays free (a line that fills
    # the terminal wraps on some consoles).
    rest = name_w + 15 + 2 + 8 + max(14, *(len(m) for m in money)) + 1
    msg_w = max(20, shutil.get_terminal_size((110, 20)).columns - rest)
    lines = [f"{'NAME':<{name_w}}{'STATE':<15}{'MESSAGE':<{msg_w + 2}}{'TIME':<8}SPENT / BUDGET"]
    for v, m in zip(views, money, strict=True):
        msg = v.message if len(v.message) <= msg_w else v.message[: msg_w - 3] + "..."
        lines.append(f"{v.name:<{name_w}}{v.state:<15}{msg:<{msg_w + 2}}{core.fmt_hours(v.used_h):<8}{m}")
    return lines


def cmd_list(args: argparse.Namespace) -> int:
    if args.watch:
        return watch(args.interval)
    views = workers.views()
    if args.json:
        print(json.dumps([asdict(v) for v in views]))
    else:
        say("\n".join(table(views)))
    return 0


def watch(interval: float) -> int:
    """list, redrawn every `interval` seconds, with an alert (and a bell) when a worker needs the user,
    finishes, fails or stops."""
    if os.name == "nt":
        os.system("")  # (turns on the escape codes that clear the screen)
    before: dict[str, str] | None = None
    alerts: list[str] = []
    try:
        while True:
            views, bell = workers.views(), False
            for v in views:
                if before is not None and before.get(v.name) != v.state and v.state in ALERTS:
                    alerts.append(f"[{time.strftime('%H:%M')}] {v.name} {ALERTS[v.state]}: {v.message}")
                    bell = True
            before = {v.name: v.state for v in views}
            screen = [
                f"crab-student - every {interval:g} s - Ctrl-C to quit - {time.strftime('%H:%M:%S')}",
                "",
                *table(views),
                *(["", *alerts[-8:]] if alerts else []),
            ]
            sys.stdout.write(
                "\033[2J\033[H" + core.ascii_text("\n".join(screen)) + "\n" + ("\a" if bell else "")
            )
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def cmd_status(args: argparse.Namespace) -> int:
    v = workers.view(args.name)
    rec = core.load(args.name)
    headline = f"{v.name}: {v.state} - {v.message}"
    if args.json:
        # The launch's line says "working on ..." or what to approve; any other state gets the headline.
        line = headline
        if v.state in ("working", "approval"):
            line = launch.result(rec, v.state, [], (rec.get("pending") or {}).get("reason"))["result_line"]
        print(json.dumps({**asdict(v), "result_line": line}))
        return 0
    say(headline)
    if v.machine:
        say(
            f"  machine   {v.machine} - ${v.dph:.2f}/hr (instance {rec['instance_id']}, {v.vast_status or 'unknown'})"
        )
    git = v.git or {}
    pushed = (
        ""
        if not git
        else " (all pushed)"
        if not (git.get("dirty") or git.get("unpushed"))
        else f" ({git.get('dirty', 0)} uncommitted, {git.get('unpushed', 0)} unpushed)"
    )
    say(f"  repo      {v.repo}, branch {v.branch}{pushed}")
    if v.limit_h:
        left = (
            f" ({core.fmt_hours(max(0, v.limit_h - (v.used_h or 0)))} left)" if v.used_h is not None else ""
        )
        idle = f"; idle {v.idle_min} min" if v.idle_min is not None else ""
        say(
            f"  time      {core.fmt_hours(v.used_h)} used of {core.fmt_hours(v.limit_h)}{left}{idle}; idle stop "
            f"after {rec.get('idle_minutes', 60)} min"
        )
        say(
            f"  money     ~{core.usd(v.spent)} spent of the {core.usd(v.budget)} budget (storage while stopped is "
            "not counted)"
        )
    if rec["phase"] == "ready":
        say(f"  open      crab-student open {v.name}    (VS Code: --vscode)")
        say(f"  results   crab-student report {v.name} | crab-student pull {v.name}")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    if args.all == bool(args.name):
        raise core.CrabError("give a worker's NAME, or --all.", 2)
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
        raise core.CrabError("no worker is running.")
    if args.vscode:
        for r in live:
            machine.open_vscode(r)
        return 0
    return machine.open_one(live[0]["name"]) if args.name else machine.open_all([r["name"] for r in live])


# ---- the parser ----------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crab-student", description="Claude Code workers on rented Vast.ai machines."
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
    c.add_argument("--hours", type=float, default=1.0, help="the task's estimated hours (default 1)")
    c.add_argument("--budget", type=float, metavar="USD")
    c.add_argument("--count", type=_positive, default=10, help="how many to show (default 10)")
    c = cmd(
        "launch", cmd_launch, "start a worker on a rented machine (the task brief on stdin)", json_flag=True
    )
    c.add_argument("name", help="3-40 characters: a-z, 0-9 and -")
    c.add_argument("--hours", type=float, required=True, help="estimated machine hours, setup included")
    _req_args(c)
    c.add_argument(
        "--budget", type=float, metavar="USD", help="the most it may cost (default: about 2x the estimate)"
    )
    c.add_argument("--brief-file", metavar="PATH", help="the task brief (else read from stdin)")
    c.add_argument("--repo", metavar="PATH", help="your repo (default: the current folder)")
    c.add_argument("--no-repo", action="store_true", help="work in your private workspace repo instead")
    c.add_argument("--secret", action="append", metavar="NAME", help="a stored secret to give the worker")
    c.add_argument("--model", help="the Claude model for the worker (default: the model setting)")
    c.add_argument("--fake-claude", action="store_true", help="run a stand-in for Claude Code (tests)")
    c.add_argument("--dry-run", action="store_true", help="plan only: rent nothing")
    for name, func, text in (
        ("approve", cmd_approve, "do what is waiting for your approval"),
        ("start", cmd_start, "start a stopped worker, or continue an interrupted launch"),
    ):
        cmd(name, func, text, json_flag=True).add_argument("name")
    cmd("decline", cmd_decline, "drop what is waiting for your approval").add_argument("name")
    c = cmd("list", cmd_list, "every worker at a glance", json_flag=True)
    c.add_argument("--watch", action="store_true", help="keep it on screen and alert on changes")
    c.add_argument("--interval", type=float, default=30, help=argparse.SUPPRESS)
    cmd("status", cmd_status, "one worker in detail", json_flag=True).add_argument("name")
    c = cmd("open", cmd_open, "attach to a worker's Claude Code in this terminal (detach: Ctrl-b d)")
    c.add_argument("name", nargs="?")
    c.add_argument("--all", action="store_true", help="every running worker side by side")
    c.add_argument("--vscode", action="store_true", help="open the machine in VS Code instead")
    c = cmd("send", cmd_send, "type a message to a worker (the text, or stdin)")
    c.add_argument("name")
    c.add_argument("text", nargs="*")
    cmd("report", cmd_report, "show a worker's REPORT.md").add_argument("name")
    c = cmd("pull", cmd_pull, "fetch a worker's branch into your repo (your checkout is not touched)")
    c.add_argument("name")
    c.add_argument("--repo", metavar="PATH")
    cmd("stop", cmd_stop, "save the worker's work, then stop its machine", json_flag=True).add_argument(
        "name"
    )
    c = cmd("extend", cmd_extend, "give a worker more machine time", json_flag=True)
    c.add_argument("name")
    c.add_argument("hours", type=float)
    c = cmd(
        "delete",
        cmd_delete,
        "destroy a worker's machine and forget it (checks for unsaved work)",
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
