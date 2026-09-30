"""Launching: the plan, the approval gate, bring-up (rent, boot, repo, upload and setup), approve,
decline, start and extend."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import shlex
import sys
import time
import uuid
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
    budget: float | None
    brief: str
    repo_path: Path | None
    no_repo: bool
    secrets: list[str]
    model: str | None
    fake_claude: bool
    dry_run: bool


class Progress:
    """Progress lines on stderr (so --json keeps stdout to one value). status() rewrites one line on a
    terminal and prints only changes elsewhere."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.tty = getattr(self.stream, "isatty", lambda: False)()
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
        print(core.ascii_text(text), file=self.stream, flush=True)

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
                print(f"{text} ({self.elapsed()})", file=self.stream, flush=True)
                self.printed = time.monotonic()
        self.shown = text


def brief_file(name: str) -> Path:
    return core.workers_dir() / f"{name}.brief.md"


# ---- launch --------------------------------------------------------------------------------------


def launch(a: LaunchArgs, out: Progress) -> dict:
    from crab_student import setup

    setup.check_ready(fake_claude=a.fake_claude)
    core.check_name(a.name)
    if core.record_path(a.name).exists():
        raise core.CrabError(
            f"a worker named {a.name} already exists. Pick another name, or delete it first: "
            f"crab-student delete {a.name}",
            2,
        )
    if not a.brief.strip():
        raise core.CrabError("the task brief is empty: pass it on stdin or with --brief-file.", 2)
    for s in a.secrets:
        if not core.SECRET_NAME_RE.fullmatch(s) or not core.get_secret(core.secret_key(s)):
            raise core.CrabError(
                f"there is no secret named {s}: run crab-student secret set {s} in a terminal.", 2
            )
    cfg = core.load_config()
    branch = f"agent/{a.name}"
    repo, repo_line, warnings = plan_repo(a, cfg, branch)
    plan = offers.plan(a.req, cfg, a.budget)
    p = plan.primary
    limit = math.floor(plan.budget_usd / p["dph_total"] * 100) / 100
    out.line(f"Planning {a.name}")
    out.line(f"  machine  {offers.summary(p)} - ${p['dph_total']:.2f}/hr (offer {p['id']})")
    if plan.fallbacks:
        top = max(o["dph_total"] for o in plan.fallbacks)
        out.line(
            f"  backups  {len(plan.fallbacks)} more offer{'s' if len(plan.fallbacks) > 1 else ''}, up to ${top:.2f}/hr"
        )
    how = f"{cfg['budget_factor']:g} x the {a.req.hours:g}h estimate" if plan.auto_budget else "yours"
    out.line(f"  budget   {core.usd(plan.budget_usd)} ({how}); stops after {core.fmt_hours(limit)}")
    out.line(f"  repo     {repo_line}")
    for w in warnings:
        out.line(f"Warning: {w}")
    if a.dry_run:
        reason = gate(plan.budget_usd, cfg, running_count())
        rec = {
            "name": a.name,
            "offers": [offers.kept(o) for o in plan.offers()],
            "req": offers.req_dict(a.req),
            "budget_usd": plan.budget_usd,
            "limit_hours": limit,
            "repo": repo,
            "pending": None,
            "dph": None,
        }
        return result(rec, "dry run", warnings, reason=reason)
    with core.locked("_gate", wait=120):  # (two launches must not both pass max_running_machines)
        if core.record_path(a.name).exists():  # (another launch took the name meanwhile)
            raise core.CrabError(f"a worker named {a.name} already exists. Pick another name.", 2)
        reason = gate(plan.budget_usd, cfg, running_count())
        rec = new_record(a, cfg, plan, repo, limit)
        rec["warnings"] = warnings  # (repeated when a launch waiting for approval goes ahead)
        if reason:
            rec.update(phase="pending", pending={"action": "launch", "reason": reason})
        brief_file(a.name).parent.mkdir(parents=True, exist_ok=True)
        brief_file(a.name).write_text(a.brief, encoding="utf-8")
        core.save(rec)
    if reason:
        return result(rec, "approval", warnings, reason=reason)
    return bring_up(a.name, out, warnings)


def plan_repo(a: LaunchArgs, cfg: dict, branch: str) -> tuple[dict, str, list[str]]:
    """Where the worker's branch starts. Nothing is written anywhere yet (the starting commit is a local,
    unreferenced object)."""
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
    sha, carried, untracked, local = github.snapshot(r, commit=not a.dry_run)
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
            f"the worker needs {it}."
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


def new_record(a: LaunchArgs, cfg: dict, plan: offers.Plan, repo: dict, limit: float) -> dict:
    return {
        "name": a.name,
        "created": core.now_iso(),
        "phase": "renting",
        "error": None,
        "req": offers.req_dict(a.req),
        "budget_usd": plan.budget_usd,
        "auto_budget": plan.auto_budget,
        "limit_hours": limit,
        "model": cfg["model"] if a.model is None else a.model,
        "fake_claude": a.fake_claude,
        "secrets": a.secrets,
        "session_id": str(uuid.uuid4()),
        "repo": repo,
        "offers": [offers.kept(o) for o in plan.offers()],
        "failed_offers": [],
        "rent": None,
        "instance_id": None,
        "dph": None,
        "machine": None,
        "short": None,
        "ssh": None,
        "uploaded": False,
        "pending": None,
        "last_seen": None,
        "image": cfg["image"],
        "idle_minutes": cfg["idle_minutes"],
        "notify_url": cfg["notify_url"],
    }


def running_count(exclude: str | None = None) -> int:
    """Machines that are running or may be: launches under way, and finished ones Vast shows running or
    on their way up."""
    recs = [r for r in core.all_records() if r["name"] != exclude]
    if not any(r["phase"] == "ready" for r in recs):
        return sum(r["phase"] in ("renting", "booting", "setup") for r in recs)
    insts = {i["id"]: i for i in vast.instances()}
    return sum(
        r["phase"] in ("renting", "booting", "setup")
        or (r["phase"] == "ready" and vast.wants_running(insts.get(r["instance_id"])))
        for r in recs
    )


def gate(budget: float | None, cfg: dict, running: int | None) -> str | None:
    """Why the user's approval is needed (None: it is not). None skips a check."""
    if budget is not None and budget > cfg["auto_approve_usd"]:
        return f"budget {core.usd(budget)} is over auto_approve_usd ({core.usd(cfg['auto_approve_usd'])})"
    if running is not None and running + 1 > cfg["max_running_machines"]:
        return f"{running} machines are running (max_running_machines is {cfg['max_running_machines']})"
    return None


# ---- bring-up ------------------------------------------------------------------------------------


def bring_up(name: str, out: Progress, warnings: list[str] | tuple = ()) -> dict:
    """Take a worker from wherever its launch got to (renting, booting, setup, failed) to ready."""
    with core.locked(name, wait=900):  # (a second caller waits for a launch at work, then reports it)
        rec = core.load(name)
        if rec["phase"] == "pending":
            raise core.CrabError(f"{name} is waiting for approval: crab-student approve {name}", 3)
        if rec["phase"] == "failed":
            replan(rec, out)
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
    """A launch cut off during setup: its machine is kept (the worker may already be at work), and may have
    stopped since; start it again (through the gate) and wait for it."""
    name, inst = rec["name"], vast.instance(rec["instance_id"])
    if inst is None:
        rec.update(phase="failed", error="the machine no longer exists on Vast", instance_id=None, ssh=None)
        core.save(rec)
        raise core.CrabError(
            f"{name}'s machine no longer exists on Vast. Try again: crab-student start {name}"
        )
    if not vast.running(inst):
        with core.locked("_gate", wait=120):
            if reason := gate(None, core.load_config(), running_count(exclude=name)):
                raise core.CrabError(f"{name}'s machine needs starting again, but {reason}. Stop another worker "
                                     "first, or raise max_running_machines.", 3)  # fmt: skip
            vast.set_state(rec["instance_id"], "running")
        rec["last_seen"] = {**(rec.get("last_seen") or {}), "stop_reason": None}
        core.save(rec)
    if why := wait_boot(rec, out, time.time() + BOOT_TIMEOUT, restarting=True):
        raise core.CrabError(
            f"{name}'s machine did not come back up ({why}). Try again later: crab-student start {name}"
        )


def replan(rec: dict, out: Progress) -> None:
    """A failed launch tried again: fresh offers within its budget."""
    plan = offers.plan(offers.Req(**rec["req"]), core.load_config(), rec["budget_usd"])
    rec.update(
        offers=[offers.kept(o) for o in plan.offers()],
        failed_offers=[],
        rent=None,
        instance_id=None,
        ssh=None,
        uploaded=False,
        phase="renting",
        error=None,
        pending=None,  # (a start that waited for approval is happening now)
    )
    core.save(rec)
    out.line(
        f"Trying {rec['name']} again with {len(rec['offers'])} fresh offer{'s' if len(rec['offers']) > 1 else ''}."
    )


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
    limit = math.floor(rec["budget_usd"] / offer["dph_total"] * 100) / 100
    rec.update(
        instance_id=iid,
        dph=offer["dph_total"],
        machine=offer["summary"],
        short=offer["short"],
        limit_hours=limit,
        phase="booting",
        ssh=None,
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
    """The worker's deploy key (usually shared, see github.claim_deploy_key) and its branch on GitHub (once;
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
        "model": rec["model"],
        "fake_claude": rec["fake_claude"],
        "session_id": rec["session_id"],
        "idle_minutes": rec["idle_minutes"],
        "limit_hours": rec["limit_hours"],
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
    """Upload and set up the machine. A setup that fails destroys the machine (the worker never ran, so it
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


# ---- approve, decline, start, extend -------------------------------------------------------------


def approve(name: str, out: Progress) -> dict:
    """Do what is waiting for approval, without the gate. A launch plans again with fresh offers, its
    approved budget as the cap."""
    with core.locked(name):
        rec = core.load(name)
        pending = rec.get("pending")
        if not pending:
            raise core.CrabError(f"{name} is not waiting for approval.")
        if pending["action"] == "launch":
            repo = rec["repo"]
            if not repo["workspace"]:
                github.check_repo(repo["slug"], repo["branch"])
            plan = offers.plan(offers.Req(**rec["req"]), core.load_config(), rec["budget_usd"])
            rec.update(
                offers=[offers.kept(o) for o in plan.offers()], failed_offers=[], rent=None, phase="renting"
            )
        rec["pending"] = None
        core.save(rec)
    if pending["action"] == "launch":
        return bring_up(name, out, rec.get("warnings", []))
    if pending["action"] == "start":
        return start(name, out, check_gate=False)
    return extend(name, pending["hours"], check_gate=False)


def decline(name: str) -> str:
    with core.locked(name):
        rec = core.load(name)
        pending = rec.get("pending")
        if not pending:
            raise core.CrabError(f"{name} is not waiting for approval.")
        if pending["action"] == "launch":
            core.remove(name)
            return f"Declined {name}: nothing was rented."
        rec["pending"] = None
        core.save(rec)
        return f"Declined: {name} stays as it was (no {pending['action']})."


def _time_up(rec: dict) -> core.CrabError:
    used = (rec.get("last_seen") or {}).get("used_hours") or rec["limit_hours"]
    return core.CrabError(
        f"{rec['name']} has reached its time limit ({core.fmt_hours(used)} used of "
        f"{core.fmt_hours(rec['limit_hours'])}). Extend it first: crab-student extend {rec['name']} 2",
        3,
    )


def start(name: str, out: Progress, check_gate: bool = True) -> dict:
    """Start a stopped worker again, or continue a launch that was interrupted or failed."""
    rec = core.load(name)
    cfg = core.load_config()
    if rec["phase"] == "pending":
        raise core.CrabError(f"{name} is waiting for approval: crab-student approve {name}", 3)
    if rec["phase"] == "failed" and check_gate and (reason := gate(None, cfg, running_count(exclude=name))):
        with core.locked(name):
            return _pend(core.load(name), "start", reason)
    if rec["phase"] != "ready":
        return bring_up(name, out)
    with core.locked(name):
        rec = core.load(name)
        inst = vast.instance(rec["instance_id"])
        if inst is None:
            raise core.CrabError(
                f"{name}'s machine no longer exists on Vast. Delete the worker: crab-student delete {name}"
            )
        if vast.running(inst):
            out.line(f"{name} is already running.")
            return result(rec, "working", [])
        if ((rec.get("last_seen") or {}).get("used_hours") or 0) >= rec["limit_hours"]:
            raise _time_up(rec)
        with core.locked("_gate", wait=120):  # (a start and a launch must not both pass the machine limit)
            if check_gate and (reason := gate(None, cfg, running_count(exclude=name))):
                return _pend(rec, "start", reason)
            vast.set_state(rec["instance_id"], "running")
        if (rec.get("pending") or {}).get("action") == "start":
            rec["pending"] = None  # (the start that waited for approval is done)
        rec["last_seen"] = {**(rec.get("last_seen") or {}), "stop_reason": None}
        core.save(rec)
        out.line(f"Starting {name}...")
        if why := wait_boot(rec, out, time.time() + BOOT_TIMEOUT, restarting=True):
            raise core.CrabError(
                f"{name} did not come back up ({why}). Vast keeps trying: check again with "
                f"crab-student status {name}, or stop it with crab-student stop {name}."
            )
        res = machine.remote(name, ["set-limit", str(rec["limit_hours"])])
        core.save(rec)
        machine.write_ssh_config()
        if res.get("used_hours", 0) >= rec["limit_hours"]:
            vast.set_state(rec["instance_id"], "stopped")
            # (remembered, so the next start refuses without booting the machine again)
            seen = {"used_hours": res["used_hours"], "stop_reason": "time-limit"}
            rec["last_seen"] = {**(rec.get("last_seen") or {}), **seen}
            core.save(rec)
            raise _time_up(rec)
        return result(rec, "working", [])


def extend(name: str, hours: float, check_gate: bool = True) -> dict:
    """More machine time: the budget grows by hours x $/hr, through the gate."""
    if not hours > 0:
        raise core.CrabError("the hours to add must be more than 0.", 2)
    with core.locked(name):
        rec = core.load(name)
        if rec["phase"] != "ready":
            raise core.CrabError(
                f"{name} has not finished launching, so there is no time limit to extend yet."
            )
        budget = core.ceil_cents(rec["budget_usd"] + hours * rec["dph"])
        if check_gate and (reason := gate(budget, core.load_config(), None)):
            return _pend(rec, "extend", reason, hours=hours)
        rec.update(budget_usd=budget, limit_hours=round(rec["limit_hours"] + hours, 2))
        core.save(rec)
        when = "when it starts again"
        if vast.running(vast.instance(rec["instance_id"])):
            try:
                machine.remote(name, ["set-limit", str(rec["limit_hours"])])
                when = "now"
            except machine.Unreachable:
                when = (
                    "when it starts again (it could not be reached now, so it keeps its old limit until then)"
                )
    return {
        **result(rec, "extended", []),
        "result_line": f"{name}: time limit is now {core.fmt_hours(rec['limit_hours'])} "
        f"(budget {core.usd(budget)}); the machine gets it {when}.",
    }


def _pend(rec: dict, action: str, reason: str, **extra: float) -> dict:
    rec["pending"] = {"action": action, "reason": reason, **extra}
    core.save(rec)
    return result(rec, "approval", [], reason=reason)


# ---- results -------------------------------------------------------------------------------------


def result(rec: dict, state: str, warnings: list[str], reason: str | None = None) -> dict:
    """What launch, approve, start and extend return (the CLI prints result_line; --json the whole)."""
    o = rec["offers"][0] if rec.get("offers") else {}
    dph, short = rec.get("dph") or o.get("dph_total"), rec.get("short") or o.get("short", "?")
    hours = rec["req"]["hours"]
    est = core.ceil_cents(dph * hours) if dph else None
    name, branch = rec["name"], rec["repo"]["branch"]
    limit = core.fmt_hours(rec["limit_hours"])
    cost = f"est. {hours:g}h ~ {core.usd(est)} - budget {core.usd(rec['budget_usd'])} (stops after {limit})"
    if state == "approval":
        action = (rec.get("pending") or {}).get("action", "launch")
        what = {
            "launch": f"would rent {short} at ${dph:.2f}/hr, {cost}",
            "start": "would start its machine again",
            "extend": "would raise its budget",
        }[action]
        line = (
            f"{name}: NOT started - needs your approval: {reason} - {what} - approve: crab-student approve {name} "
            f"(or: crab-student decline {name})"
        )
    elif state == "dry run":
        need = f"needs approval ({reason})" if reason else "no approval needed"
        line = f"{name} (dry run): would rent {short} at ${dph:.2f}/hr - {cost} - {need}"
    else:
        line = f"{name}: working on {short} at ${dph:.2f}/hr - {cost} - branch {branch} - watch: crab-student open {name}"
    return {
        "name": name,
        "state": state,
        "result_line": line,
        "warnings": warnings,
        "needs_approval": state == "approval" or bool(reason),
        "instance_id": rec.get("instance_id"),
        "machine": rec.get("machine") or o.get("summary"),
        "dph": dph,
        "hours": hours,
        "est_usd": est,
        "budget_usd": rec["budget_usd"],
        "limit_hours": rec["limit_hours"],
        "repo": rec["repo"]["slug"],
        "branch": branch,
        "exit_code": 3 if state == "approval" else 0,
    }
