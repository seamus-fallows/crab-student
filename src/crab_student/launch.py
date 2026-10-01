"""Launching: the plan, the approval rule, bring-up (rent, boot, repo, upload and setup), approve, decline and
start."""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import shlex
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from crab_student import core, github, machine, offers, vast

BOOT_TIMEOUT = 15 * 60  # (a stopped machine started again: Vast may wait for its host to have room)
START_TIMEOUT = 5 * 60  # a new machine Vast does not report running this long after its rent is given up
SSH_TIMEOUT = 3 * 60  # and one whose ssh does not answer this long after it runs
RACERS = 2  # new machines rented at once for one launch
POLL = 5.0
LABEL_WAIT = 120.0
LABEL_PREFIX = "crab-student-"  # (every rent's label: crab-student-NAME-<8 hex>)

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
    setup: str | None = None  # the command installing the project's dependencies (None: detected; "none")


class Progress:
    """Progress lines on stderr (so --json keeps stdout to one value), and with the time in the agent's launch
    log (`log_file`) once attach() names the agent (earlier lines are kept until then)."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.shown: str | None = None  # (the last status line printed)
        self.t0 = self.printed = time.monotonic()
        self.name: str | None = None
        self.early: list[str] = []  # (log lines from before the agent's record existed)

    def elapsed(self) -> str:
        s = round(time.monotonic() - self.t0)
        return f"{s // 60}m{s % 60:02d}s"

    def attach(self, name: str) -> None:
        """From now on, log to NAME's launch log (with the lines so far)."""
        if self.name != name:
            self.name = name
            for text in self.early:
                append_log(name, text)
            self.early = []

    def _log(self, text: str) -> None:
        if self.name:
            append_log(self.name, text)
        else:
            self.early.append(text)

    def line(self, text: str) -> None:
        self.shown = None
        self._log(text)
        print(core.ascii_text(text), file=self.stream, flush=True)

    def status(self, text: str) -> None:
        """A changing line, with the time so far: printed (and logged, marked "~") when its first part changes, or
        when its detail changes and 15 s have passed since the last one printed (an image pull reports often)."""
        text = core.ascii_text(text)
        new_part = text.split(" - ")[0] != (self.shown or "").split(" - ")[0]
        if text == self.shown or not (new_part or time.monotonic() - self.printed >= 15):
            return
        self.shown, self.printed = text, time.monotonic()
        self._log(f"~ {text} ({self.elapsed()})")
        print(f"{text} ({self.elapsed()})", file=self.stream, flush=True)


def log_file(name: str) -> Path:
    return core.agents_dir() / f"{name}.log"


def append_log(name: str, text: str) -> None:
    """One line of the agent's launch log, with the local time ("~ " starts a status line)."""
    with contextlib.suppress(OSError), open(log_file(name), "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {core.ascii_text(text)}\n")


def brief_file(name: str) -> Path:
    return core.agents_dir() / f"{name}.brief.md"


# ---- launch --------------------------------------------------------------------------------------


def launch(a: LaunchArgs, out: Progress) -> dict:
    from crab_student import setup

    setup.check_ready(fake_claude=a.fake_claude)
    check_new(a)
    cfg = core.load_config()
    repo, repo_line, warnings = plan_repo(a, cfg, f"agent/{a.name}")
    plan = offers.plan(a.req, cfg, cfg["approve_above_usd_per_hour"], mode=a.mode or cfg["mode"])
    p = plan.primary
    reason = approval_reason(plan, cfg["approve_above_usd_per_hour"])
    rec = new_record(a, cfg, plan, repo, reason)
    out.line(f"Planning {a.name}")
    out.line(f"  machine  {offers.summary(p)} - ${p['dph_total']:.2f}/hr (offer {p['id']})")
    if plan.fallbacks:
        top = max(o["dph_total"] for o in plan.fallbacks)
        n = len(plan.fallbacks)
        out.line(
            f"  backups  {n} more offer{'s' if n > 1 else ''}, up to ${top:.2f}/hr (the first is rented at the "
            "same time: the first machine ready is kept)"
        )
    out.line(f"  agent    {rec['mode']} - Claude Code: {settings_text(rec['claude_settings'])}")
    out.line(f"  repo     {repo_line}")
    out.line(f"  setup    {setup_text(a.setup)}")
    for w in warnings:
        out.line(f"Warning: {w}")
    if a.dry_run:
        return result(rec, "dry run", warnings, reason=reason)
    save_new(rec, a.brief, warnings)
    out.attach(a.name)
    if reason:
        return result(rec, "approval", warnings, reason=reason)
    show_launch(a.name, out)
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


def plan_repo(a: LaunchArgs, cfg: dict, branch: str) -> tuple[dict, str, list[str]]:
    """Where the agent's branch starts. Nothing is written anywhere yet (the starting commit is a local,
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
    Claude Code settings; only those set somewhere. The user's own effort comes with its per-model levels
    (`modelSettings`), which a chosen effort replaces."""
    own, fast_setting = core.own_claude_settings(), {"on": True, "off": False}.get(cfg["fast_mode"])
    effort = a.effort or cfg["effort"]
    chosen = {
        "model": a.model or cfg["model"] or own.get("model"),
        "effortLevel": effort or own.get("effortLevel"),
        "modelSettings": None if effort else own.get("modelSettings"),
        "fastMode": next((v for v in (a.fast, fast_setting, own.get("fastMode")) if v is not None), None),
    }
    return {k: v for k, v in chosen.items() if v is not None}


def settings_text(s: dict) -> str:
    """ "model opus, effort high, fast mode on" (an effort per model: "high for claude-opus-5-5"), or what
    Claude Code does by itself."""
    parts = [f"model {s['model']}"] if s.get("model") else []
    effort = [s["effortLevel"]] if s.get("effortLevel") else []
    effort += [f"{v['effortLevel']} for {m}" for m, v in (s.get("modelSettings") or {}).items()]
    parts += [f"effort {', '.join(effort)}"] if effort else []
    parts += [f"fast mode {'on' if s['fastMode'] else 'off'}"] if "fastMode" in s else []
    return ", ".join(parts) or "its defaults"


def setup_text(setup: str | None) -> str:
    if not setup:
        return "the project's dependencies installed as the repo says (detected on the machine)"
    return (
        "no dependency install" if setup == "none" else f"the project's dependencies installed with: {setup}"
    )


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
        "setup": a.setup,
        "secrets": a.secrets,
        "session_id": str(uuid.uuid4()),
        "repo": repo,
        "offers": [offers.kept(o) for o in plan.offers()],
        "failed_offers": [],
        "rents": [],  # (the rents in flight while machines race: label, offer_id, at, instance_id, running_at)
        "rent": None,  # (the kept machine's rent)
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
        log_file(rec["name"]).write_text("", encoding="utf-8")
        core.save(rec)


def show_launch(name: str, out: Progress) -> None:
    """Open a window that follows the launch and then shows the agent (unless the open_window setting is off),
    else say how to follow it."""
    if core.load_config()["open_window"] == "on" and machine.open_launch_window(name):
        out.line(f"A window follows the launch of {name} (again any time: crab-student open {name})")
    else:
        out.line(f"Follow the launch of {name} in a terminal: crab-student open {name}")


# ---- bring-up ------------------------------------------------------------------------------------


def bring_up(name: str, out: Progress, warnings: list[str] | tuple = ()) -> dict:
    """Bring an agent's launch to ready from wherever it got: a new launch races, one cut off during setup goes
    on, and one that failed or was cut off before its upload is planned afresh. Its progress, and a failure,
    also go to its launch log."""
    with core.locked(name, wait=900):  # (a second caller waits for a launch at work, then reports it)
        out.attach(name)
        rec = core.load(name)
        if rec["phase"] == "pending":
            raise core.CrabError(f"{name} is waiting for approval: crab-student approve {name}", 3)
        try:
            return _bring_up(rec, out, list(warnings))
        except core.CrabError as e:
            out.line(f"Launch failed: {e.message}")
            raise


def _bring_up(rec: dict, out: Progress, warnings: list[str]) -> dict:
    cut_off = rec["phase"] in ("renting", "booting") and bool(rec["rents"] or rec["instance_id"])
    if cut_off:
        out.line("The launch was cut off before its upload: destroying its machines (they hold nothing).")
        lost = drop_unuploaded(rec)
        for w in lost:
            out.line(f"Warning: {w}")
        warnings += lost
    if cut_off or rec["phase"] == "failed":
        replan(rec, out)
        if rec["phase"] == "pending":
            return result(rec, "approval", warnings, reason=rec["pending"]["reason"])
    if rec["phase"] == "renting":
        warnings += Race(rec, out).run()
    if rec["phase"] == "setup":
        resume_machine(rec, out)
    if rec["phase"] in ("booting", "setup"):
        prepare_repo(rec, out)
        warnings += setup_machine(rec, out)
    machine.write_ssh_config()
    out.line(f"Ready after {out.elapsed()}.")
    return result(rec, "working", warnings)


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
    if why := wait_boot(rec, out, time.time() + BOOT_TIMEOUT):
        raise core.CrabError(
            f"{name}'s machine did not come back up ({why}). Try again later: crab-student start {name}"
        )


def replan(rec: dict, out: Progress) -> None:
    """A failed launch tried again: fresh offers up to the price it may rent at, else it waits for approval."""
    cfg = core.load_config()
    cap = max(rec.get("max_dph") or 0, cfg["approve_above_usd_per_hour"])
    plan = offers.plan(offers.Req(**rec["req"]), cfg, cap, mode=rec["mode"])
    rec.update(
        offers=[offers.kept(o) for o in plan.offers()],
        failed_offers=[],
        rents=[],
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


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:g} minute{'' if seconds == 60 else 's'}"


class Race:
    """Rent the record's offers RACERS at a time and keep the first machine that is ready (Vast says running and
    ssh answers), destroying the others at once: nothing was uploaded to any. A machine not running
    START_TIMEOUT after its rent, not answering ssh SSH_TIMEOUT after it runs, or failing is destroyed and the
    next unused offer rented in its place. Each rent's label is saved in `rents` before its request is sent. A
    rent whose outcome is unknown is never sent again: it keeps its place in the race until its machine shows
    up by that label. The race is over within this command: a rent still unconfirmed when another machine wins,
    or when only such rents are left after LABEL_WAIT (the launch fails), becomes a warning to look out for its
    machine."""

    def __init__(self, rec: dict, out: Progress):
        self.rec, self.out = rec, out
        self.seen: dict[str, tuple[str, str]] = {}  # label -> the last (status, message) seen
        self.errors: dict[str, int] = {}  # label -> polls in a row with an error in the status message
        self.keyed: set[str] = set()  # labels whose machine has been given the ssh key
        self.looking: dict[str, float] = {}  # label -> when this run began to look for its machine
        self.no_credit: str | None = None

    def run(self) -> list[str]:
        """The warnings (unconfirmed rents left behind) once a machine is kept."""
        rec = self.rec
        while True:
            self.fill()
            if not rec["rents"]:
                self.fail()
            if any(r["instance_id"] for r in rec["rents"]):
                prepare_repo(rec, self.out)  # (once, while the machines start)
            for r in list(rec["rents"]):
                verdict, inst = self.check(r)
                if verdict == "ready":
                    return self.win(r, inst)
                if verdict:
                    self.drop(r, verdict)
            if self.stuck():
                self.give_up()
            self.show()
            time.sleep(POLL)

    def stuck(self) -> bool:
        """Only unconfirmed rents are in flight (they hold every racing slot, or no offer is left), and none of
        their machines has shown up for LABEL_WAIT."""
        rents, now = self.rec["rents"], time.monotonic()
        return bool(rents) and all(
            r["instance_id"] is None and now - self.looking[r["label"]] >= LABEL_WAIT for r in rents
        )

    def fill(self) -> None:
        """Rent unused offers until RACERS are in flight (or none is left, or the account has no credit)."""
        rec = self.rec
        first = not (rec["rents"] or rec["failed_offers"])
        while len(rec["rents"]) < RACERS and not self.no_credit:
            used = {r["offer_id"] for r in rec["rents"]} | {f["id"] for f in rec["failed_offers"]}
            free = [o for o in rec["offers"] if o["id"] not in used][: RACERS - len(rec["rents"])]
            if not free:
                return
            said = [f"offer {o['id']} ({o['short']}, ${o['dph_total']:.2f}/hr)" for o in free]
            if len(free) > 1:
                self.out.line(
                    f"Renting two machines at once: {' and '.join(said)}; keeping the first that starts."
                )
            else:
                self.out.line(f"Renting {said[0]}{'' if first else ' in its place'}.")
            for offer in free:
                if not self.no_credit:
                    self.rent(offer)
            first = False

    def rent(self, offer: dict) -> None:
        rec = self.rec
        r = {"label": f"{LABEL_PREFIX}{rec['name']}-{secrets.token_hex(4)}", "offer_id": offer["id"],
             "at": time.time(), "instance_id": None, "running_at": None}  # fmt: skip
        rec["rents"].append(r)
        core.save(rec)  # (the label is saved before the request)
        try:
            r["instance_id"] = vast.rent(offer["id"], rent_body(rec, offer, r["label"]))
        except vast.VastError as e:
            rec["rents"].remove(r)
            if e.status == 402 or NO_CREDIT.search(e.message):
                self.no_credit = e.message
            else:
                self.out.line(f"  offer {offer['id']} is not available ({e.message}).")
                rec["failed_offers"].append({"id": offer["id"], "reason": e.message})
            core.save(rec)
            return
        except vast.RentUnknown as e:
            self.out.line(
                f"  Vast did not confirm the rent of offer {offer['id']} ({e}); looking for it by its label."
            )
            return
        core.save(rec)
        self.out.line(f"  offer {offer['id']}: instance {r['instance_id']}")

    def check(self, r: dict) -> tuple[str | None, dict | None]:
        """("ready", the instance), (a reason to give the machine up, None), or (None, None) to wait."""
        rec, now = self.rec, time.time()
        if r["instance_id"] is None:
            iid = find_by_label(r["label"])
            if iid is None:
                self.looking.setdefault(r["label"], time.monotonic())
                self.seen[r["label"]] = ("not confirmed", "")
                return None, None
            r["instance_id"] = iid
            core.save(rec)
            self.out.line(f"  offer {r['offer_id']}: found instance {iid} by its label")
        inst = vast.instance(r["instance_id"])
        if inst is None:
            return "disappeared", None
        actual, msg = (
            inst.get("actual_status") or "waiting",
            " ".join(str(inst.get("status_msg") or "").split()),
        )
        self.seen[r["label"]] = (actual, msg)
        if actual == "running":
            if not r["running_at"]:
                r["running_at"] = now
                core.save(rec)
            if addr := vast.address(inst):
                if r["label"] not in self.keyed:
                    vast.add_ssh_key(r["instance_id"], (core.ssh_dir() / "id_ed25519.pub").read_text())
                    self.keyed.add(r["label"])
                if machine.reachable(rec["name"], addr):
                    return "ready", inst
            if now > r["running_at"] + SSH_TIMEOUT:
                return f"did not answer ssh within {_minutes(SSH_TIMEOUT)} of starting", None
            return None, None
        if actual in ("exited", "offline"):
            return f"failed to start (status {actual}{': ' + msg if msg else ''})", None
        error = re.search(r"\b(error|failed)\b", msg, re.I)
        self.errors[r["label"]] = self.errors.get(r["label"], 0) + 1 if error else 0
        if self.errors[r["label"]] >= 2:
            return f"failed to start ({msg})", None
        if now > r["at"] + START_TIMEOUT:
            last = f"{actual}{' - ' + msg if msg else ''}"
            return f"did not start within {_minutes(START_TIMEOUT)} (last status: {last})", None
        return None, None

    def show(self) -> None:
        seen = [(r["offer_id"], *self.seen.get(r["label"], ("renting", ""))) for r in self.rec["rents"]]
        if len(seen) == 1:
            _, actual, msg = seen[0]
            self.out.status(f"Booting: {actual}{' - ' + msg[:80] if msg else ''}")
        elif seen:
            head = ", ".join(f"offer {o} {actual}" for o, actual, _ in seen)
            detail = "; ".join(f"offer {o}: {msg[:50]}" for o, _, msg in seen if msg)
            self.out.status(f"Booting: {head}{' - ' + detail if detail else ''}")

    def drop(self, r: dict, why: str) -> None:
        vast.destroy(r["instance_id"])
        self.rec["rents"].remove(r)
        self.rec["failed_offers"].append({"id": r["offer_id"], "reason": why})
        core.save(self.rec)
        self.out.line(f"Offer {r['offer_id']} {why}: destroyed it.")

    def win(self, r: dict, inst: dict) -> list[str]:
        """Keep this machine: destroy the others first (each still recorded until it is gone), then take it. The
        warnings: rents Vast did not confirm whose machine has not shown up."""
        rec, destroyed, warnings = self.rec, [], []
        for other in [x for x in rec["rents"] if x is not r]:
            if iid := other["instance_id"] or find_by_label(other["label"]):
                vast.destroy(iid)
                destroyed.append(f"offer {other['offer_id']}")
            else:
                warnings.append(unconfirmed(other))
            rec["rents"].remove(other)
            core.save(rec)
        offer = next(o for o in rec["offers"] if o["id"] == r["offer_id"])
        storage = offer.get("storage_cost")
        rec.update(
            rents=[],
            rent=r,
            instance_id=r["instance_id"],
            dph=offer["dph_total"],
            disk_usd_day=round(storage * rec["req"]["disk_gb"] / 30, 3) if storage else None,
            machine=offer["summary"],
            short=offer["short"],
            phase="booting",
            ssh=None,
            last_start=r["at"],
        )
        machine.set_address(rec, inst)
        core.save(rec)
        machine.forget_host_key(rec["name"])  # (a new machine for this name has a new host key)
        machine.write_ssh_config()
        gone = f": destroyed {' and '.join(destroyed)}" if destroyed else ""
        self.out.line(f"Offer {r['offer_id']} is ready after {self.out.elapsed()}{gone}.")
        for w in warnings:
            self.out.line(f"Warning: {w}")
        return warnings

    def give_up(self) -> None:
        """Only unconfirmed rents are left and none of their machines has shown up: the launch fails (start plans
        it afresh)."""
        rec = self.rec
        message = rent_unknown(rec)
        drop_unuploaded(rec)  # (a machine that shows up meanwhile is destroyed)
        rec.update(phase="failed", error=message)
        core.save(rec)
        raise core.CrabError(message)

    def fail(self) -> None:
        rec, name = self.rec, self.rec["name"]
        if self.no_credit:
            rec.update(phase="failed", error=self.no_credit)
            core.save(rec)
            raise core.CrabError(
                "your Vast account has no credit left. Add credit at https://cloud.vast.ai/billing/, then: "
                f"crab-student start {name}"
            )
        reasons = "; ".join(f"{f['id']}: {f['reason']}" for f in rec["failed_offers"][-3:])
        rec.update(phase="failed", error=f"no offer could be rented or started ({reasons})")
        core.save(rec)
        raise core.CrabError(
            f"every offer failed ({reasons}). Try again later with crab-student start {name}, or "
            "loosen the requirements.",
            4,
        )


def rent_unknown(rec: dict) -> str:
    """Why a race gave up: Vast confirmed none of the rents left, and none of their machines has appeared."""
    which = " and ".join(f"offer {r['offer_id']}" for r in rec["rents"])
    labels = " or ".join(r["label"] for r in rec["rents"])
    return (
        f"Vast did not confirm the rent of {which}, and no machine labelled {labels} has appeared. Try again: "
        f"crab-student start {rec['name']} (if a machine labelled {labels} shows up later, crab-student list "
        "points it out: destroy it at https://cloud.vast.ai/instances/)."
    )


def unconfirmed(rent: dict) -> str:
    """The warning about a rent Vast did not confirm whose machine has not shown up."""
    return (
        f"Vast did not confirm the rent of offer {rent['offer_id']}; if a machine labelled {rent['label']} shows "
        "up, crab-student list points it out (destroy it at https://cloud.vast.ai/instances/)."
    )


def find_by_label(label: str) -> int | None:
    return next((i["id"] for i in vast.instances() if i.get("label") == label), None)


def rent_body(rec: dict, offer: dict, label: str) -> dict:
    env = {"CS_VAST_URL": vast.base_url()}
    if not rec["req"]["gpus"] and offer["gpu_host"]:
        env["NVIDIA_DISABLE_REQUIRE"] = "1"  # (a CPU task on a GPU host: start whatever the driver)
    return {
        **RENT_FIXED,
        "image": rec["image"],
        "disk": float(rec["req"]["disk_gb"]),
        "label": label,
        "onstart": ONSTART,
        "env": env,
    }


def wait_boot(rec: dict, out: Progress, deadline: float) -> str | None:
    """Wait until the agent's machine, started again, runs and answers ssh: None, else why not. It reads "exited"
    until its host has room, so only the deadline gives up on it."""
    key_added, began = False, time.time()
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
        if time.time() > deadline:
            last = f"{actual}{': ' + msg if msg else ''}"
            return f"not up after {_minutes(deadline - began)} (last status: {last})"
        time.sleep(POLL)


def drop_unuploaded(rec: dict) -> list[str]:
    """Destroy the launch's machines that never received anything: those racing (found by label when Vast did
    not confirm the rent) and the kept machine before its upload. The warnings: rents whose machine was not
    found."""
    warnings = []
    for r in rec["rents"]:
        if iid := r["instance_id"] or find_by_label(r["label"]):
            vast.destroy(iid)
        else:
            warnings.append(unconfirmed(r))
    rec["rents"] = []
    if rec["instance_id"] and not rec["uploaded"]:
        vast.destroy(rec["instance_id"])
        rec.update(instance_id=None, ssh=None)
    core.save(rec)
    return warnings


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
        warnings = drop_unuploaded(rec)
        rec.update(phase="failed", error=e.message)
        core.save(rec)
        message = (
            f"{e.message} (the machines rented for it were destroyed: they held nothing). Fix that, then: "
            f"crab-student start {name}"
        )
        raise core.CrabError(" ".join([message, *warnings])) from None
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
        "setup": rec["setup"],
        # (an interactive agent's machine gets the server of the VS Code here, so the first open is quick)
        "vscode_commit": machine.local_vscode() if rec["mode"] == "interactive" else None,
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


def setup_machine(rec: dict, out: Progress) -> list[str]:
    """Upload and set up the machine. A setup that fails destroys the machine (the agent never ran, so it
    holds nothing); a lost connection leaves the launch for `start` to continue."""
    files = generated_files(rec)
    rec.update(phase="setup", uploaded=True)
    core.save(rec)  # (saved before the upload: a partial upload may already hold secrets and a checkout)
    try:
        warnings = machine.upload_and_setup(
            rec["name"],
            machine.bundle(files, fake_claude=rec["fake_claude"]),
            lambda line: out.line(f"Setting up {line[5:]}"),
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
    for w in warnings:
        out.line(f"Warning: {w}")
    return warnings


# ---- approve, decline, start ---------------------------------------------------------------------


def approve(name: str, out: Progress) -> dict:
    """Launch an agent waiting for approval, with fresh offers up to the price the user approved; one whose price
    rose meanwhile waits again."""
    with core.locked(name):
        rec = core.load(name)
        pending = rec.get("pending")
        if not pending:
            raise core.CrabError(f"{name} is not waiting for approval.")
        repo = rec["repo"]
        if not repo["workspace"] and not repo["branch_created"]:  # (a new launch: are the checks still true?)
            github.check_repo(repo["slug"], repo["branch"])
        plan = offers.plan(offers.Req(**rec["req"]), core.load_config(), pending["max_dph"], mode=rec["mode"])
        if plan.over_cap:  # (prices went up while it waited: ask again)
            reason = approval_reason(plan, pending["max_dph"])
            rec["pending"] = {"reason": reason, "max_dph": plan.top_price()}
            core.save(rec)
            return result(rec, "approval", rec.get("warnings", []), reason=reason)
        rec.update(
            offers=[offers.kept(o) for o in plan.offers()],
            failed_offers=[],
            rents=[],
            phase="renting",
            pending=None,
            max_dph=pending["max_dph"],
        )
        core.save(rec)
    show_launch(name, out)
    return bring_up(name, out, rec.get("warnings", []))


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
        show_launch(name, out)
        return bring_up(name, out)
    with core.locked(name):
        out.attach(name)
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
        if why := wait_boot(rec, out, time.time() + BOOT_TIMEOUT):
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
