"""Launching: the plan, the approval rule, bring-up (rent, boot, repo, upload and setup), several launches at
once, approve, decline and start."""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from crab_student import core, github, machine, offers, vast

BOOT_TIMEOUT = 15 * 60
POLL = 5.0
LABEL_WAIT = 120.0

# Vast runs this as root at every container start. It saves the container's own Vast key (only this
# machine can use it), starts the dead-man switch (stop the machine once neither this start nor the
# watchdog's heartbeat is newer than 45 minutes) and starts the watchdog once setup has been done.
# Every command ends in ";" or "&", so it works whether Vast keeps the line breaks or not.
_STOP_CALL = (
    'curl -s -m 30 -X PUT -H "Authorization: Bearer $CONTAINER_API_KEY" -H "Content-Type: application/json" '
    '-d "{\\"state\\":\\"stopped\\"}" "${CS_VAST_URL:-https://console.vast.ai}/api/v0/instances/$CONTAINER_ID/"'
)
ONSTART = "\n".join(
    [
        "D=/root/crab-student; mkdir -p $D/logs; cd $D; umask 077;",
        "env | grep -E '^(CONTAINER_ID|CONTAINER_API_KEY|CS_VAST_URL)=' | sed 's/^/export /' > vast.env;",
        "date +%s > booted;",
        "nohup bash -c 'cd /root/crab-student; while sleep 60; do t=$(cat booted); "
        'h=$(stat -c %Y heartbeat 2>/dev/null || echo 0); [ "$h" -gt "$t" ] && t=$h; '
        f"if [ $(( $(date +%s) - t )) -gt 2700 ]; then . ./vast.env; {_STOP_CALL}; fi; done' "
        "> logs/deadman.log 2>&1 &",
        '[ -f setup-done ] && nohup "$(command -v python3 || echo /venv/main/bin/python)" cs.py watch '
        ">> logs/watch.log 2>&1 &",
    ]
)

RENT_FIXED = {
    "client_id": "me",
    "price": None,
    "extra": None,
    "image_login": None,
    "python_utf8": False,
    "lang_utf8": False,
    "use_jupyter_lab": False,
    "jupyter_dir": None,
    "force": False,
    "cancel_unavail": True,
    "template_hash_id": None,
    "user": None,
    "runtype": "ssh_direc ssh_proxy",
}
NO_CREDIT = re.compile(r"credit|insufficient.?(funds|balance)", re.I)


@dataclass
class LaunchArgs:
    name: str
    req: offers.Req
    brief: str
    repo_path: Path | None
    no_repo: bool
    secrets: list[str]
    fake_claude: bool
    dry_run: bool
    mode: str | None = None  # None: the mode setting
    model: str | None = None  # model, effort, fast: None falls back to the settings, then to the user's own
    effort: str | None = None
    fast: bool | None = None


class Progress:
    """Progress lines on stderr (so --json keeps stdout to one value). status() rewrites one line on a
    terminal and prints only changes elsewhere. A prefix ("[job1] ") marks the lines of one of several
    launches at once (never rewritten: they share the terminal)."""

    def __init__(self, stream=None, prefix: str = ""):
        self.stream = stream or sys.stderr
        self.prefix = prefix
        self.tty = not prefix and getattr(self.stream, "isatty", lambda: False)()
        if self.tty and os.name == "nt":
            os.system("")  # (turns on the escape codes that rewrite the status line)
        self.shown: str | None = None
        self.t0 = self.printed = time.monotonic()

    def elapsed(self) -> str:
        s = round(time.monotonic() - self.t0)
        return f"{s // 60}m{s % 60:02d}s"

    def line(self, text: str) -> None:
        if self.tty and self.shown is not None:
            self.stream.write("\n")
        self.shown = None
        print(core.ascii_text(self.prefix + text), file=self.stream, flush=True)

    def status(self, text: str) -> None:
        """One changing line: rewritten in place (with the time so far) on a terminal; elsewhere printed when
        its first part changes, and a detail change at most every 15 s (an image pull reports often)."""
        text = core.ascii_text(text)
        if self.tty:
            self.stream.write(f"\r\033[K{text} ({self.elapsed()})")
            self.stream.flush()
        elif text != self.shown:
            new_part = text.split(" - ")[0] != (self.shown or "").split(" - ")[0]
            if new_part or time.monotonic() - self.printed >= 15:
                print(f"{self.prefix}{text} ({self.elapsed()})", file=self.stream, flush=True)
                self.printed = time.monotonic()
        self.shown = text


def brief_file(name: str) -> Path:
    return core.agents_dir() / f"{name}.brief.md"


# ---- launch --------------------------------------------------------------------------------------


def launch(a: LaunchArgs, out: Progress) -> dict:
    from crab_student import setup

    setup.check_ready(fake_claude=a.fake_claude)
    check_new(a)
    cfg = core.load_config()
    repo, repo_line, warnings = plan_repo(a, cfg, f"agent/{a.name}")
    plan = offers.plan(a.req, cfg, cfg["approve_above_usd_per_hour"])
    p = plan.primary
    reason = approval_reason(plan, cfg["approve_above_usd_per_hour"])
    rec = new_record(a, cfg, plan, repo, reason)
    out.line(f"Planning {a.name}")
    out.line(f"  machine  {offers.summary(p)} - ${p['dph_total']:.2f}/hr (offer {p['id']})")
    if plan.fallbacks:
        top = max(o["dph_total"] for o in plan.fallbacks)
        out.line(
            f"  backups  {len(plan.fallbacks)} more offer{'s' if len(plan.fallbacks) > 1 else ''}, up to ${top:.2f}/hr"
        )
    out.line(f"  agent    {rec['mode']} - Claude Code: {settings_text(rec['claude_settings'])}")
    out.line(f"  repo     {repo_line}")
    for w in warnings:
        out.line(f"Warning: {w}")
    if a.dry_run:
        return result(rec, "dry run", warnings, reason=reason)
    save_new(rec, a.brief, warnings)
    if reason:
        return result(rec, "approval", warnings, reason=reason)
    return bring_up(a.name, out, warnings)


def check_new(a: LaunchArgs) -> None:
    core.check_name(a.name)
    if core.record_path(a.name).exists():
        raise core.CrabError(
            f"an agent named {a.name} already exists. Pick another name, or delete it first: "
            f"crab-student delete {a.name}",
            2,
        )
    if not a.brief.strip():
        raise core.CrabError(f"the task brief for {a.name} is empty.", 2)
    for s in a.secrets:
        if not core.SECRET_NAME_RE.fullmatch(s) or not core.get_secret(core.secret_key(s)):
            raise core.CrabError(
                f"there is no secret named {s}: run crab-student secret set {s} in a terminal.", 2
            )


def approval_reason(plan: offers.Plan, cap: float) -> str | None:
    """Why the user must approve the launch first: nothing that fits costs `cap` $/hr or less."""
    if not plan.over_cap:
        return None
    return (
        f"it would cost ${plan.primary['dph_total']:.2f}/hr, and no machine that fits costs ${cap:.2f}/hr "
        "or less (approve_above_usd_per_hour)"
    )


def plan_repo(
    a: LaunchArgs, cfg: dict, branch: str, snapshots: dict | None = None
) -> tuple[dict, str, list[str]]:
    """Where the agent's branch starts. Nothing is written anywhere yet (the starting commit is a local,
    unreferenced object; several launches at once share one through `snapshots`)."""
    if a.no_repo:
        slug = f"{github.login()}/{cfg['workspace_repo']}"
        github.check_workspace(slug, branch)  # (a repo that does not exist yet is created after the rent)
        repo = {
            "slug": slug,
            "workspace": True,
            "branch": branch,
            "start_sha": None,
            "deploy_key": None,
            "branch_created": False,
            "dir": f"/root/work/{cfg['workspace_repo']}",
        }
        return repo, f"{slug} (your workspace repo) - {branch}", []
    r = github.local_repo(a.repo_path or Path.cwd())
    github.check_repo(r.slug, branch)
    snapshots = {} if snapshots is None else snapshots
    if str(r.top) not in snapshots:
        snapshots[str(r.top)] = github.snapshot(r, commit=not a.dry_run)
    sha, carried, untracked, local = snapshots[str(r.top)]
    base = (
        github.git(r.top, "symbolic-ref", "-q", "--short", "HEAD", check=False).strip() or "a detached HEAD"
    )
    line = f"{r.slug} - {branch} starts from {r.head[:7]} (your HEAD on {base})"
    if local:
        line += f" + {local} local commit{'s' if local > 1 else ''} not on GitHub"
    if carried:
        line += f" + uncommitted changes to {_files(carried)}"
    warnings = []
    if untracked:
        it = "it" if len(untracked) == 1 else "them"
        warnings.append(
            f"not carried over: {_files(untracked, 'untracked file')}. Commit {it} and launch again if "
            f"the agent needs {it}."
        )
    repo = {
        "slug": r.slug,
        "workspace": False,
        "branch": branch,
        "start_sha": sha,
        "local_dir": str(r.top),
        "carried": carried,
        "untracked": untracked,
        "deploy_key": None,
        "branch_created": False,
        "dir": f"/root/work/{r.slug.split('/')[1]}",
    }
    return repo, line, warnings


def _files(paths: list[str], word: str = "file") -> str:
    shown = ", ".join(paths[:3]) + (", ..." if len(paths) > 3 else "")
    return f"{len(paths)} {word}{'s' if len(paths) > 1 else ''} ({shown})"


def claude_settings(a: LaunchArgs, cfg: dict) -> dict:
    """The agent's Claude Code settings: each from the launch's flag, else the setting, else the user's own
    Claude Code settings; only those set somewhere."""
    own, fast_setting = core.own_claude_settings(), {"on": True, "off": False}.get(cfg["fast_mode"])
    chosen = {
        "model": a.model or cfg["model"] or own.get("model"),
        "effortLevel": a.effort or cfg["effort"] or own.get("effortLevel"),
        "fastMode": next((v for v in (a.fast, fast_setting, own.get("fastMode")) if v is not None), None),
    }
    return {k: v for k, v in chosen.items() if v is not None}


def settings_text(s: dict) -> str:
    """ "model opus, effort high, fast mode on", or what Claude Code does by itself."""
    parts = [f"model {s['model']}"] if s.get("model") else []
    parts += [f"effort {s['effortLevel']}"] if s.get("effortLevel") else []
    parts += [f"fast mode {'on' if s['fastMode'] else 'off'}"] if "fastMode" in s else []
    return ", ".join(parts) or "its defaults"


def new_record(a: LaunchArgs, cfg: dict, plan: offers.Plan, repo: dict, reason: str | None) -> dict:
    return {
        "name": a.name,
        "created": core.now_iso(),
        "phase": "pending" if reason else "renting",
        "error": None,
        "req": offers.req_dict(a.req),
        "max_dph": cfg["approve_above_usd_per_hour"],  # (the most it may rent at without asking again)
        "mode": a.mode or cfg["mode"],
        "claude_settings": claude_settings(a, cfg),
        "fake_claude": a.fake_claude,
        "secrets": a.secrets,
        "session_id": str(uuid.uuid4()),
        "repo": repo,
        "offers": [offers.kept(o) for o in plan.offers()],
        "failed_offers": [],
        "rent": None,
        "instance_id": None,
        "dph": None,
        "disk_usd_day": None,
        "machine": None,
        "short": None,
        "ssh": None,
        "uploaded": False,
        "pending": {"reason": reason, "max_dph": plan.top_price()} if reason else None,
        "last_start": None,
        "stopped_since": None,
        "stop_note": None,
        "last_seen": None,
        "image": cfg["image"],
        "idle_minutes": cfg["idle_minutes"],
        "notify_url": cfg["notify_url"],
    }


def save_new(rec: dict, brief: str, warnings: list[str]) -> None:
    with core.locked(rec["name"]):
        if core.record_path(rec["name"]).exists():  # (another launch took the name meanwhile)
            raise core.CrabError(f"an agent named {rec['name']} already exists. Pick another name.", 2)
        rec["warnings"] = warnings  # (repeated when a launch waiting for approval goes ahead)
        brief_file(rec["name"]).parent.mkdir(parents=True, exist_ok=True)
        brief_file(rec["name"]).write_text(brief, encoding="utf-8")
        core.save(rec)


# ---- several at once -----------------------------------------------------------------------------


def launch_batch(items: list[LaunchArgs], out: Progress) -> dict:
    """Launch several agents at once. Everything is planned first (a problem stops the batch before anything
    is rented), each agent picks a different machine, and the bring-ups run side by side."""
    from crab_student import setup

    if len({a.name for a in items}) < len(items):
        raise core.CrabError("two agents in the batch have the same name.", 2)
    setup.check_ready(fake_claude=any(a.fake_claude for a in items))
    for a in items:
        check_new(a)
    cfg = core.load_config()
    cap, taken, found, snapshots, planned = cfg["approve_above_usd_per_hour"], set(), {}, {}, []
    for a in items:
        repo, _, warnings = plan_repo(a, cfg, f"agent/{a.name}", snapshots)
        key = json.dumps(offers.req_dict(a.req), sort_keys=True)
        if key not in found:  # (one search per kind of machine)
            found[key] = offers.search(a.req, cfg)
        plan = offers.plan(a.req, cfg, cap, taken, found[key])
        taken.add(plan.primary["id"])  # (backups may be another's pick: a taken offer moves on to the next)
        reason = approval_reason(plan, cap)
        rec, p = new_record(a, cfg, plan, repo, reason), plan.primary
        out.line(
            f"{a.name}: {offers.short(p)} at ${p['dph_total']:.2f}/hr - {rec['mode']}"
            + (" - needs your approval" if reason else "")
        )
        planned.append((a, rec, warnings, reason))
    if any(a.dry_run for a in items):
        return batch_result([result(rec, "dry run", w, reason=r) for _, rec, w, r in planned])
    for a, rec, warnings, _ in planned:
        save_new(rec, a.brief, warnings)
    jobs = [
        (rec["name"], lambda o, name=rec["name"], w=warnings: bring_up(name, o, w))
        for _, rec, warnings, reason in planned
        if not reason
    ]
    done = {r["name"]: r for r in side_by_side(jobs, out)}
    return batch_result(
        [done.get(rec["name"]) or result(rec, "approval", w, reason=r) for _, rec, w, r in planned]
    )


def side_by_side(jobs: list, out: Progress) -> list[dict]:
    """Run each (name, job) in its own thread, its progress lines marked with its name. A job that fails gives a
    failed result instead of stopping the others."""

    def run(name: str, job) -> dict:
        try:
            return job(Progress(out.stream, prefix=f"[{name}] "))
        except Exception as e:  # (one agent's trouble, even a bug, must not lose the others' results)
            return failed(name, e)

    if not jobs:
        return []
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        return list(pool.map(lambda job: run(*job), jobs))


def failed(name: str, e: Exception) -> dict:
    message, code = (e.message, e.code) if isinstance(e, core.CrabError) else (repr(e), 1)
    line = f"{name}: launch failed - {message}"
    return {"name": name, "state": "failed", "result_line": line, "warnings": [], "exit_code": code}


def batch_result(results: list[dict]) -> dict:
    """Several launches' results as one: every line, the warnings once each, and the worst exit code."""
    codes = [r["exit_code"] for r in results]
    bad = [c for c in codes if c not in (0, 3)]
    return {
        "state": "batch",
        "results": results,
        "result_line": "\n".join(r["result_line"] for r in results),
        "warnings": list(dict.fromkeys(w for r in results for w in r.get("warnings", []))),
        "exit_code": bad[0] if bad else 3 if 3 in codes else 0,
    }


# ---- bring-up ------------------------------------------------------------------------------------


def bring_up(name: str, out: Progress, warnings: list[str] | tuple = ()) -> dict:
    """Take an agent from wherever its launch got to (renting, booting, setup, failed) to ready."""
    with core.locked(name, wait=900):  # (a second caller waits for a launch at work, then reports it)
        rec = core.load(name)
        if rec["phase"] == "pending":
            raise core.CrabError(f"{name} is waiting for approval: crab-student approve {name}", 3)
        if rec["phase"] == "failed":
            replan(rec, out)
            if rec["phase"] == "pending":
                return result(rec, "approval", list(warnings), reason=rec["pending"]["reason"])
        if rec["phase"] == "renting":
            rent_loop(rec, out)
        elif rec["phase"] == "booting" and (why := wait_boot(rec, out, rec["rent"]["at"] + BOOT_TIMEOUT)):
            discard(rec, why, out)
            rent_loop(rec, out)
        if rec["phase"] == "setup":
            resume_machine(rec, out)
        if rec["phase"] in ("booting", "setup"):
            prepare_repo(rec, out)
            setup_machine(rec, out)
        machine.write_ssh_config()
        out.line(f"Ready after {out.elapsed()}.")
        return result(rec, "working", list(warnings))


def resume_machine(rec: dict, out: Progress) -> None:
    """A launch cut off during setup: its machine is kept (the agent may already be at work), and may have
    stopped since; start it again and wait for it."""
    name, inst = rec["name"], vast.instance(rec["instance_id"])
    if inst is None:
        rec.update(phase="failed", error="the machine no longer exists on Vast", instance_id=None, ssh=None)
        core.save(rec)
        raise core.CrabError(
            f"{name}'s machine no longer exists on Vast. Try again: crab-student start {name}"
        )
    if not vast.running(inst):
        vast.set_state(rec["instance_id"], "running")
        rec["last_seen"] = {**(rec.get("last_seen") or {}), "stop_reason": None}
        rec.update(last_start=time.time(), stopped_since=None)
        core.save(rec)
    if why := wait_boot(rec, out, time.time() + BOOT_TIMEOUT, restarting=True):
        raise core.CrabError(
            f"{name}'s machine did not come back up ({why}). Try again later: crab-student start {name}"
        )


def replan(rec: dict, out: Progress) -> None:
    """A failed launch tried again: fresh offers up to the price it may rent at, else it waits for approval."""
    cfg = core.load_config()
    cap = max(rec.get("max_dph") or 0, cfg["approve_above_usd_per_hour"])
    plan = offers.plan(offers.Req(**rec["req"]), cfg, cap)
    rec.update(
        offers=[offers.kept(o) for o in plan.offers()],
        failed_offers=[],
        rent=None,
        instance_id=None,
        ssh=None,
        uploaded=False,
        phase="renting",
        error=None,
        pending=None,
    )
    if plan.over_cap:
        rec.update(
            phase="pending", pending={"reason": approval_reason(plan, cap), "max_dph": plan.top_price()}
        )
    core.save(rec)
    if not plan.over_cap:
        n = len(rec["offers"])
        out.line(f"Trying {rec['name']} again with {n} fresh offer{'s' if n > 1 else ''}.")


def rent_loop(rec: dict, out: Progress) -> None:
    """Rent the first offer that works; never send a rent twice when its outcome is unknown."""
    failed = {f["id"] for f in rec["failed_offers"]}
    for offer in [o for o in rec["offers"] if o["id"] not in failed]:
        if rec["rent"] and rec["rent"]["offer_id"] == offer["id"]:  # sent by an earlier run: outcome unknown
            iid = find_by_label(rec["rent"]["label"], 0)
            if iid is None:
                raise core.CrabError(rent_unknown(rec))
        else:
            rec["rent"] = {
                "label": f"crab-student-{rec['name']}-{secrets.token_hex(4)}",
                "offer_id": offer["id"],
                "at": time.time(),
            }
            rec["phase"] = "renting"
            core.save(rec)  # (the label is saved before the request)
            out.line(f"Renting offer {offer['id']} ({offer['short']}, ${offer['dph_total']:.2f}/hr)...")
            try:
                iid = vast.rent(offer["id"], rent_body(rec, offer))
            except vast.VastError as e:
                if e.status == 402 or NO_CREDIT.search(e.message):
                    rec.update(phase="failed", error=e.message, rent=None)
                    core.save(rec)
                    raise core.CrabError(
                        "your Vast account has no credit left. Add credit at "
                        f"https://cloud.vast.ai/billing/, then: crab-student start {rec['name']}"
                    ) from None
                out.line(f"  offer {offer['id']} is not available ({e.message}); trying the next one.")
                rec["failed_offers"].append({"id": offer["id"], "reason": e.message})
                rec["rent"] = None
                core.save(rec)
                continue
            except vast.RentUnknown as e:
                out.line(f"  Vast did not confirm the rent ({e}); looking for the machine by its label...")
                iid = find_by_label(rec["rent"]["label"], LABEL_WAIT)
                if iid is None:
                    raise core.CrabError(rent_unknown(rec)) from None
        adopt(rec, iid, offer)
        out.line(f"  rented: instance {iid}")
        prepare_repo(rec, out)
        why = wait_boot(rec, out, rec["rent"]["at"] + BOOT_TIMEOUT)
        if why is None:
            return
        discard(rec, why, out)
    reasons = "; ".join(f"{f['id']}: {f['reason']}" for f in rec["failed_offers"][-3:])
    rec.update(phase="failed", error=f"no offer could be rented or booted ({reasons})")
    core.save(rec)
    raise core.CrabError(
        f"every offer failed ({reasons}). Try again later with crab-student start {rec['name']}, or "
        "loosen the requirements.",
        4,
    )


def rent_unknown(rec: dict) -> str:
    r = rec["rent"]
    return (
        f"Vast did not confirm the rent of offer {r['offer_id']} and no machine labelled {r['label']} has appeared. "
        "crab-student will not rent again by itself, so you do not pay twice. Run crab-student start "
        f"{rec['name']} in a few minutes to look again, or crab-student delete {rec['name']} to give up (then "
        f"destroy any machine labelled {r['label']} at https://cloud.vast.ai/instances/)."
    )


def find_by_label(label: str, wait: float) -> int | None:
    deadline = time.monotonic() + wait
    while True:
        found = [i["id"] for i in vast.instances() if i.get("label") == label]
        if found:
            return found[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(10.0, max(POLL, 0.01)))


def rent_body(rec: dict, offer: dict) -> dict:
    env = {"CS_VAST_URL": vast.base_url()}
    if not rec["req"]["gpus"] and offer["gpu_host"]:
        env["NVIDIA_DISABLE_REQUIRE"] = "1"  # (a CPU task on a GPU host: start whatever the driver)
    return {
        **RENT_FIXED,
        "image": rec["image"],
        "disk": float(rec["req"]["disk_gb"]),
        "label": rec["rent"]["label"],
        "onstart": ONSTART,
        "env": env,
    }


def adopt(rec: dict, iid: int, offer: dict) -> None:
    storage = offer.get("storage_cost")
    rec.update(
        instance_id=iid,
        dph=offer["dph_total"],
        disk_usd_day=round(storage * rec["req"]["disk_gb"] / 30, 3) if storage else None,
        machine=offer["summary"],
        short=offer["short"],
        phase="booting",
        ssh=None,
        last_start=time.time(),
    )
    core.save(rec)
    machine.forget_host_key(rec["name"])


def wait_boot(rec: dict, out: Progress, deadline: float, restarting: bool = False) -> str | None:
    """Wait until the machine runs and answers ssh: None, else why not. A machine being started again
    reads "exited" until its host has room, so only a new one fails on that (or on a boot error)."""
    key_added, errors = False, 0
    while True:
        inst = vast.instance(rec["instance_id"])
        if inst is None:
            return "the machine disappeared"
        actual, msg = (
            inst.get("actual_status") or "waiting",
            " ".join(str(inst.get("status_msg") or "").split()),
        )
        out.status(f"Booting: {actual}{' - ' + msg[:80] if msg else ''}")
        if actual == "running" and vast.address(inst):
            if machine.set_address(rec, inst):
                core.save(rec)
                machine.write_ssh_config()
            if not key_added:
                vast.add_ssh_key(rec["instance_id"], (core.ssh_dir() / "id_ed25519.pub").read_text())
                key_added = True
            if machine.reachable(rec["name"]):
                out.line(f"Booted: ssh answers ({out.elapsed()})")
                return None
        elif not restarting:
            if actual in ("exited", "offline"):
                return f"status {actual}: {msg}"
            errors = errors + 1 if re.search(r"\b(error|failed)\b", msg, re.I) else 0
            if errors >= 2:
                return msg
        if time.time() > deadline:
            return f"not running after {BOOT_TIMEOUT // 60} minutes (last status: {actual}{': ' + msg if msg else ''})"
        time.sleep(POLL)


def discard(rec: dict, why: str, out: Progress) -> None:
    """Give up on a machine that never received anything: destroy it and move on to the next offer."""
    assert not rec["uploaded"]
    out.line(f"Offer {rec['rent']['offer_id']} did not boot ({why}): destroyed it, trying the next offer.")
    vast.destroy(rec["instance_id"])
    rec["failed_offers"].append({"id": rec["rent"]["offer_id"], "reason": why})
    rec.update(instance_id=None, ssh=None, rent=None, phase="renting")
    core.save(rec)
    machine.write_ssh_config()


def prepare_repo(rec: dict, out: Progress) -> None:
    """The agent's deploy key (usually shared, see github.claim_deploy_key) and its branch on GitHub (once;
    the record says what is done)."""
    repo, name = rec["repo"], rec["name"]
    if repo["branch_created"]:
        return
    try:
        if repo["workspace"] and not repo["start_sha"]:
            github.ensure_workspace(*repo["slug"].split("/"))
            repo["start_sha"] = github.default_head(repo["slug"])
        if not repo["deploy_key"]:
            github.claim_deploy_key(rec)
        github.create_branch(repo, github.key_file(repo["deploy_key"]))
    except core.CrabError as e:
        if rec["instance_id"] and not rec["uploaded"]:
            vast.destroy(rec["instance_id"])
            rec.update(instance_id=None, ssh=None)
        rec.update(phase="failed", error=e.message)
        core.save(rec)
        raise core.CrabError(
            f"{e.message} (the machine was destroyed: it held nothing). Fix that, then: "
            f"crab-student start {name}"
        ) from None
    repo["branch_created"] = True
    core.save(rec)
    out.line(f"GitHub: {repo['branch']} created on {repo['slug']}")


def machine_config(rec: dict) -> dict:
    return {
        "name": rec["name"],
        "branch": rec["repo"]["branch"],
        "repo_url": github.git_url(rec["repo"]["slug"]),
        "repo_dir": rec["repo"]["dir"],
        "mode": rec["mode"],
        "claude_settings": rec["claude_settings"],
        "fake_claude": rec["fake_claude"],
        "session_id": rec["session_id"],
        "idle_minutes": rec["idle_minutes"],
        "notify_url": rec["notify_url"],
        "secret_names": rec["secrets"],
    }


def generated_files(rec: dict) -> dict[str, tuple[bytes, int]]:
    token = "" if rec["fake_claude"] else core.get_secret(core.TOKEN)
    if token is None:
        raise core.CrabError("no Claude token is stored. Run: crab-student init --claude-token")
    values = {s: core.get_secret(core.secret_key(s)) for s in rec["secrets"]}
    if missing := [s for s, v in values.items() if not v]:
        raise core.CrabError(
            f"the secret {missing[0]} is no longer stored: crab-student secret set {missing[0]}"
        )
    claude_env = f"export CLAUDE_CODE_OAUTH_TOKEN={shlex.quote(token)}\n" if token else ""
    return {
        "config.json": (json.dumps(machine_config(rec), indent=2).encode(), 0o600),
        "BRIEF.md": (brief_file(rec["name"]).read_bytes(), 0o644),
        "claude.env": (claude_env.encode(), 0o600),
        "secrets.env": ("".join(f"export {k}={shlex.quote(v)}\n" for k, v in values.items()).encode(), 0o600),
        "deploy_key": (
            github.key_file(rec["repo"]["deploy_key"]).read_bytes().replace(b"\r\n", b"\n"),
            0o600,
        ),
        "github_known_hosts": ((github.HOST_KEY + "\n").encode(), 0o644),
    }


def setup_machine(rec: dict, out: Progress) -> None:
    """Upload and set up the machine. A setup that fails destroys the machine (the agent never ran, so it
    holds nothing); a lost connection leaves the launch for `start` to continue."""
    files = generated_files(rec)
    rec.update(phase="setup", uploaded=True)
    core.save(rec)  # (saved before the upload: a partial upload may already hold secrets and a checkout)
    try:
        machine.upload_and_setup(
            rec["name"], machine.bundle(files), lambda line: out.line(f"Setting up {line[5:]}")
        )
    except machine.Unreachable as e:
        raise core.CrabError(
            f"{e.message} The launch can continue: crab-student start {rec['name']}"
        ) from None
    except core.CrabError as e:
        vast.destroy(rec["instance_id"])
        rec.update(phase="failed", error=e.message, instance_id=None, ssh=None)
        core.save(rec)
        machine.write_ssh_config()
        raise core.CrabError(
            f"{e.message}. The machine was destroyed (a failed setup holds no work). Retry: "
            f"crab-student start {rec['name']}",
            5,
        ) from None
    rec["phase"] = "ready"
    core.save(rec)


# ---- approve, decline, start ---------------------------------------------------------------------


def approve(name: str, out: Progress) -> dict:
    return approve_all([name], out)


def approve_all(names: list[str], out: Progress) -> dict:
    """Launch agents waiting for approval, each with fresh offers (a different machine each) up to the price
    the user approved; one whose price rose meanwhile waits again."""
    names, taken, results = list(dict.fromkeys(names)), set(), {}
    for name in names:
        try:
            if waiting := _approve_plan(name, taken):
                results[name] = waiting
        except core.CrabError as e:
            if len(names) == 1:
                raise
            results[name] = failed(name, e)
    if len(names) == 1:
        name = names[0]
        return results[name] if results else bring_up(name, out, core.load(name).get("warnings", []))
    go = [n for n in names if n not in results]
    jobs = [(n, lambda o, n=n: bring_up(n, o, core.load(n).get("warnings", []))) for n in go]
    results.update({r["name"]: r for r in side_by_side(jobs, out)})
    return batch_result([results[n] for n in names])


def _approve_plan(name: str, taken: set) -> dict | None:
    """Plan an approved launch again (fresh offers, none in `taken`): None when it may go, else the result
    saying it waits for approval again."""
    with core.locked(name):
        rec = core.load(name)
        pending = rec.get("pending")
        if not pending:
            raise core.CrabError(f"{name} is not waiting for approval.")
        repo = rec["repo"]
        if not repo["workspace"] and not repo["branch_created"]:  # (a new launch: are the checks still true?)
            github.check_repo(repo["slug"], repo["branch"])
        plan = offers.plan(offers.Req(**rec["req"]), core.load_config(), pending["max_dph"], taken)
        if plan.over_cap:  # (prices went up while it waited: ask again)
            reason = approval_reason(plan, pending["max_dph"])
            rec["pending"] = {"reason": reason, "max_dph": plan.top_price()}
            core.save(rec)
            return result(rec, "approval", rec.get("warnings", []), reason=reason)
        taken.add(plan.primary["id"])
        rec.update(
            offers=[offers.kept(o) for o in plan.offers()],
            failed_offers=[],
            rent=None,
            phase="renting",
            pending=None,
            max_dph=pending["max_dph"],
        )
        core.save(rec)
    return None


def decline(name: str) -> str:
    with core.locked(name):
        if not core.load(name).get("pending"):
            raise core.CrabError(f"{name} is not waiting for approval.")
        core.remove(name)
    return f"Declined {name}: nothing was rented."


def start(name: str, out: Progress) -> dict:
    """Start a stopped agent again, or continue a launch that was interrupted or failed."""
    rec = core.load(name)
    if rec["phase"] == "pending":
        raise core.CrabError(f"{name} is waiting for approval: crab-student approve {name}", 3)
    if rec["phase"] != "ready":
        return bring_up(name, out)
    with core.locked(name):
        rec = core.load(name)
        inst = vast.instance(rec["instance_id"])
        if inst is None:
            raise core.CrabError(
                f"{name}'s machine no longer exists on Vast. Delete the agent: crab-student delete {name}"
            )
        if vast.running(inst):
            out.line(f"{name} is already running.")
            return result(rec, "working", [])
        vast.set_state(rec["instance_id"], "running")
        rec["last_seen"] = {**(rec.get("last_seen") or {}), "stop_reason": None}
        rec.update(last_start=time.time(), stopped_since=None)
        core.save(rec)
        out.line(f"Starting {name}...")
        if why := wait_boot(rec, out, time.time() + BOOT_TIMEOUT, restarting=True):
            raise core.CrabError(
                f"{name} did not come back up ({why}). Vast keeps trying: check again with "
                f"crab-student status {name}, or stop it with crab-student stop {name}."
            )
        machine.write_ssh_config()
        return result(rec, "working", [])


# ---- results -------------------------------------------------------------------------------------


def result(rec: dict, state: str, warnings: list[str], reason: str | None = None) -> dict:
    """What launch, approve and start return (the CLI prints result_line; --json the whole)."""
    o = rec["offers"][0] if rec.get("offers") else {}
    dph, short = rec.get("dph") or o.get("dph_total"), rec.get("short") or o.get("short", "?")
    name, branch, mode = rec["name"], rec["repo"]["branch"], rec["mode"]
    if state == "approval":
        line = (
            f"{name}: NOT started - needs your approval: {reason} - would rent {short} at ${dph:.2f}/hr - "
            f"approve: crab-student approve {name} (or: crab-student decline {name})"
        )
    elif state == "dry run":
        need = f"needs approval ({reason})" if reason else "no approval needed"
        line = f"{name} (dry run): would rent {short} at ${dph:.2f}/hr - {mode} - {need}"
    elif mode == "interactive":
        line = (
            f"{name}: ready on {short} at ${dph:.2f}/hr - interactive - branch {branch} - open it: "
            f"crab-student open {name} --vscode"
        )
    else:
        line = f"{name}: working on {short} at ${dph:.2f}/hr - {mode} - branch {branch} - watch: crab-student open {name}"
    return {
        "name": name,
        "state": state,
        "result_line": line,
        "warnings": warnings,
        "needs_approval": state == "approval" or bool(reason),
        "instance_id": rec.get("instance_id"),
        "machine": rec.get("machine") or o.get("summary"),
        "dph": dph,
        "mode": mode,
        "repo": rec["repo"]["slug"],
        "branch": branch,
        "exit_code": 3 if state == "approval" else 0,
    }
