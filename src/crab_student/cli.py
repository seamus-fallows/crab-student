"""The `crab-student` command: the parser, the handlers and all terminal output."""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import re
import shutil
import statistics
import sys
import time
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


# ---- argument types ------------------------------------------------------------------------------


def _amount(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a number, 0 or more")
    return value


def _hours(text: str) -> float:
    if not _amount(text):
        raise argparse.ArgumentTypeError("must be a number of hours above 0")
    return float(text)


def _gpus(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", text.strip())
    if not m or int(m.group(2) or m.group(1)) < int(m.group(1)):
        raise argparse.ArgumentTypeError("a count (2) or a range (2-8)")
    return int(m.group(1)), int(m.group(2) or m.group(1))


def _offer_ids(text: str) -> list[int]:
    ids = [i.strip() for i in text.split(",")]
    if not all(i.isdigit() and int(i) > 0 for i in ids):
        raise argparse.ArgumentTypeError("offer ids separated by commas, the pick first: 123,456,789")
    return [int(i) for i in ids]


def _words(text: str) -> list[str]:
    return [w.strip() for w in text.split(",") if w.strip()]


def _positive(text: str) -> int:
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError("must be a whole number, 1 or more")
    return int(text)


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


SORTS = {
    "price": lambda v: (v["usd_per_hour"], v["id"]),
    "cpus": lambda v: -(v["vcpus"] or 0),
    "down": lambda v: -v["down_mbps"],
    "reliability": lambda v: -(v["reliability"] or 0),
}


def cmd_offers(args: argparse.Namespace) -> int:
    cfg = core.load_config()
    f = offers.Filters(
        gpus=args.gpus,
        gpu_ram_gb=args.gpu_ram,
        gpu_models=_words(args.gpu_models),
        min_compute_cap=args.min_compute_cap,
        max_compute_cap=args.max_compute_cap,
        cpus=int(args.cpus),
        ram_gb=args.ram,
        disk_gb=args.disk or cfg["disk_gb"],
        min_down_mbps=args.min_down,
        min_up_mbps=args.min_up,
        max_price=args.max_price,
        regions=_words(args.region),
        hours=args.hours,
    )
    now = time.time()
    found = sorted((offers.shown(o, now) for o in offers.search(f, cfg)), key=SORTS[args.sort])
    if not found:
        raise core.CrabError("no offer fits these filters right now: loosen them.", 4)
    kinds = offer_kinds(found, f.cpu_task) if args.kinds else None
    if args.json:
        print(json.dumps({"kinds": kinds} if kinds else {"count": len(found), "offers": found[: args.limit]}))
        return 0
    say("\n".join(kinds_table(kinds, f.cpu_task) if kinds else map(offer_line, found[: args.limit])))
    floor = (
        f" (no GPU used: at least {offers.CPU_TASK_CPUS} vCPUs and {offers.CPU_TASK_RAM_GB} GB of RAM)"
        if f.cpu_task
        else ""
    )
    say(f"\n{len(found)} offers fit{floor}. Prices include {f.disk_gb:g} GB of disk. Nothing was rented.")
    return 0


def _n(x: float | None) -> str:
    return "?" if x is None else f"{x:.4g}"


def offer_line(v: dict) -> str:
    gpu = (
        f"{v['gpus']}x {v['gpu']} {v['gpu_ram_gb']:g}GB cc{v['compute_cap']:g} {_n(v['gpu_mem_bw_gbps'])}GB/s "
        f"pcie {_n(v['pcie_gbps'])}GB/s{' nvlink' if v['nvlink'] else ''}"
    )
    lease = "?" if v["lease_days"] is None else f"{v['lease_days']:g}d"
    return (
        f"{v['id']:<9} m{v['machine']:<7} ${v['usd_per_hour']:<6.3f} {gpu} | {_n(v['vcpus'])}/{_n(v['host_vcpus'])} "
        f"vCPU {v['cpu'] or '?'} | {v['ram_gb']}GB RAM | disk {v['disk_gb']}GB {v['disk_mbps']}MB/s | "
        f"net {v['down_mbps']}/{v['up_mbps']} Mbps, ${v['down_usd_per_tb']:g}/${v['up_usd_per_tb']:g} per TB "
        f"down/up | reliability {_n(v['reliability'])} | {v['location'] or '?'} | lease {lease}"
    )


def offer_kinds(found: list[dict], cpu_task: bool) -> list[dict]:
    """The offers grouped by kind of GPU (count, model, memory), or by CPU model for a task that uses no GPU,
    cheapest first."""
    groups: dict[str, list[dict]] = {}
    for v in found:
        key = (v["cpu"] or "?") if cpu_task else f"{v['gpus']}x {v['gpu']} {v['gpu_ram_gb']:g}GB"
        groups.setdefault(key, []).append(v)
    kinds = []
    for key, vs in groups.items():
        prices = [v["usd_per_hour"] for v in vs]
        kind = {"kind": key, "offers": len(vs), "min_usd_per_hour": min(prices),
                "median_usd_per_hour": statistics.median(prices)}  # fmt: skip
        if cpu_task:
            cpus, ram = [v["vcpus"] or 0 for v in vs], [v["ram_gb"] for v in vs]
            whole = sum(bool(v["host_vcpus"]) and (v["vcpus"] or 0) >= v["host_vcpus"] for v in vs)
            kind.update(vcpus=[min(cpus), max(cpus)], ram_gb=[min(ram), max(ram)], whole_cpu=whole)
        else:
            kind.update(compute_cap=vs[0]["compute_cap"], gpu_mem_bw_gbps=vs[0]["gpu_mem_bw_gbps"])
        kinds.append(kind)
    return sorted(kinds, key=lambda k: k["min_usd_per_hour"])


def kinds_table(kinds: list[dict], cpu_task: bool) -> list[str]:
    width = max(12, *(len(k["kind"]) for k in kinds)) + 2
    if cpu_task:
        lines = [
            f"{'CPU':<{width}}{'OFFERS':>6}  {'FROM $/HR':>9}  {'MEDIAN':>7}  {'VCPUS':>9}  {'RAM GB':>10}  WHOLE CPU"
        ]
        for k in kinds:
            cpus, ram = "-".join(_n(x) for x in k["vcpus"]), "-".join(map(str, k["ram_gb"]))
            lines.append(
                f"{k['kind']:<{width}}{k['offers']:>6}  {k['min_usd_per_hour']:>9.3f}  {k['median_usd_per_hour']:>7.3f}"
                f"  {cpus:>9}  {ram:>10}  {k['whole_cpu']} of {k['offers']}"
            )
        return lines
    lines = [f"{'GPUS':<{width}}{'OFFERS':>6}  {'FROM $/HR':>9}  {'MEDIAN':>7}  {'CC':>4}  MEM BW GB/S"]
    for k in kinds:
        lines.append(
            f"{k['kind']:<{width}}{k['offers']:>6}  {k['min_usd_per_hour']:>9.3f}  {k['median_usd_per_hour']:>7.3f}"
            f"  {k['compute_cap']:>4g}  {_n(k['gpu_mem_bw_gbps'])}"
        )
    return lines


def cmd_launch(args: argparse.Namespace) -> int:
    if args.repo and args.no_repo:
        raise core.CrabError("use either --repo or --no-repo, not both.", 2)
    res = launch.launch(_launch_args(args, _read_brief(args)), launch.Progress())
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


def _launch_args(args: argparse.Namespace, brief: str) -> launch.LaunchArgs:
    return launch.LaunchArgs(
        name=args.name,
        offer_ids=args.offers,
        disk_gb=args.disk or core.load_config()["disk_gb"],
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
        setup=_setup(args.setup),
    )


def _setup(text: str | None) -> str | None:
    if text is not None and not text.strip():
        raise core.CrabError('--setup needs a command (or "none" for no dependency install).', 2)
    return text.strip() if text else None


def _agent_args(p: argparse.ArgumentParser) -> None:
    """The agent's own flags."""
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
    p.add_argument(
        "--setup",
        metavar="COMMAND",
        help="how the machine installs the project's dependencies during setup, before the agent starts (default: "
        "uv sync, pip install -e . or pip install -r requirements*.txt, as the repo says; none: no install)",
    )


def cmd_approve(args: argparse.Namespace) -> int:
    res = launch.approve(args.name, launch.Progress())
    emit(res, args.json)
    return res["exit_code"]


def cmd_decline(args: argparse.Namespace) -> int:
    say(launch.decline(args.name))
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    res = launch.start(args.name, launch.Progress(), args.offers)
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
    views, orphans = agents.listing()
    if args.json:
        print(json.dumps([asdict(v) for v in views]))
    else:
        say("\n".join(table(views)))
    for inst in orphans:
        say(
            f"warning: machine {inst['id']} ({inst['label']}) belongs to no agent - Vast may not have confirmed "
            "its rent. Destroy it at https://cloud.vast.ai/instances/",
            err=True,
        )
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
    if args.vscode:
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
        "offers", cmd_offers, "the machines on offer that fit, cheapest first (rents nothing)", json_flag=True
    )
    c.add_argument(
        "--gpus",
        type=_gpus,
        default=(0, 0),
        metavar="N|N-M",
        help="GPUs the task uses, a count or a range (default 0: no GPU used, any machine with at least "
        f"{offers.CPU_TASK_CPUS} vCPUs and {offers.CPU_TASK_RAM_GB} GB of RAM)",
    )
    c.add_argument("--gpu-ram", type=_amount, default=0, metavar="GB", help="memory per GPU")
    c.add_argument("--gpu-models", default="", metavar="LIST", help='GPU models, e.g. "4090,5090,A100"')
    c.add_argument(
        "--min-compute-cap", type=_amount, default=0, metavar="CC", help="e.g. 8.0: Ampere or newer"
    )
    c.add_argument("--max-compute-cap", type=_amount, default=0, metavar="CC", help="e.g. 9.0: no Blackwell")
    c.add_argument("--cpus", type=_amount, default=0, help="vCPUs")
    c.add_argument("--ram", type=_amount, default=0, metavar="GB", help="RAM")
    c.add_argument(
        "--disk", type=_amount, metavar="GB", help="disk, in the prices (default: the disk_gb setting)"
    )
    c.add_argument("--min-down", type=_amount, default=0, metavar="MBPS", help="download speed")
    c.add_argument("--min-up", type=_amount, default=0, metavar="MBPS", help="upload speed")
    c.add_argument("--max-price", type=_amount, default=0, metavar="USD", help="$/hr")
    c.add_argument("--region", default="", help="continents (europe, north-america, ...) or country codes")
    c.add_argument(
        "--hours",
        type=_hours,
        help="for a job that may run over a day: the host's lease must last twice as long",
    )
    c.add_argument("--sort", choices=list(SORTS), default="price", help="(default: price)")
    c.add_argument("--limit", type=_positive, default=30, help="how many offers to show (default 30)")
    c.add_argument(
        "--kinds", action="store_true", help="a line per kind of GPU (CPU model, if no GPU is used)"
    )
    c = cmd(
        "launch", cmd_launch, "start an agent on a rented machine (the task brief on stdin)", json_flag=True
    )
    c.add_argument("name", help="3-40 characters: a-z, 0-9 and -")
    c.add_argument(
        "--offers",
        type=_offer_ids,
        required=True,
        metavar="IDS",
        help="the offers to rent (see crab-student offers): the pick, then its backups, e.g. 123,456,789",
    )
    c.add_argument("--disk", type=_amount, metavar="GB", help="disk (default: the disk_gb setting)")
    c.add_argument("--brief-file", metavar="PATH", help="the task brief (else read from stdin)")
    c.add_argument("--repo", metavar="PATH", help="your repo (default: the current folder)")
    c.add_argument("--no-repo", action="store_true", help="work in your private workspace repo instead")
    _agent_args(c)
    c.add_argument("--fake-claude", action="store_true", help="run a stand-in for Claude Code (tests)")
    c.add_argument("--dry-run", action="store_true", help="plan only: rent nothing")
    cmd("approve", cmd_approve, "launch an agent waiting for your approval", json_flag=True).add_argument(
        "name"
    )
    c = cmd("start", cmd_start, "start a stopped agent, or continue an interrupted launch", json_flag=True)
    c.add_argument("name")
    c.add_argument(
        "--offers",
        type=_offer_ids,
        metavar="IDS",
        help="for a launch that failed or was cut off before its upload: other offers to rent, the pick first",
    )
    cmd("decline", cmd_decline, "drop a launch waiting for your approval").add_argument("name")
    cmd("list", cmd_list, "every agent at a glance", json_flag=True)
    cmd("status", cmd_status, "one agent in detail", json_flag=True).add_argument("name")
    c = cmd(
        "open",
        cmd_open,
        "attach to an agent's Claude Code in this terminal (detach: Ctrl-b d); while it is launching, follow the "
        "launch first; an interactive agent opens in VS Code, and this terminal attaches after its /handoff",
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
    # Set when run from inside VS Code (its terminal, or Claude Code in it). Inherited, it makes Code.exe, started
    # by the vscode:// link that fills in the Claude panel, run as Node and fail ("bad option: --open-url").
    os.environ.pop("ELECTRON_RUN_AS_NODE", None)
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
