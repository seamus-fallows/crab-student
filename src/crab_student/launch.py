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
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from crab_student import core, github, machine, offers, vast

BOOT_TIMEOUT = 15 * 60  # (a stopped machine started again: Vast may wait for its host to have room)
# (most image pulls finish within 10 minutes, and a host that hangs silently must not hold up the next offer longer)
START_TIMEOUT = 10 * 60  # a new machine Vast does not report running this long after its rent is given up
SSH_TIMEOUT = 3 * 60  # and one whose ssh does not answer this long after it runs
POLL = 5.0
LABEL_WAIT = 120.0
# The record's fields about the launch's machine, while it has none (`label`: the rent's, saved before the request).
NO_MACHINE = {"label": None, "instance_id": None, "ssh": None, "uploaded": False, "dph": None, "disk_usd_day": None,
              "machine": None, "short": None}  # fmt: skip

# Vast runs this as root at every container start. It saves the container's own Vast key (only this
# machine can use it), and the stop call's header with it, starts the dead-man switch (stop the machine
# once neither this start nor the watchdog's heartbeat is newer than 45 minutes) and starts the watchdog
# once setup has been done. The key stays off every command line: printf is the shell's own, and curl
# reads the header from its file. Every command ends in ";" or "&", so it works whether Vast keeps the
# line breaks or not.
_STOP_CALL = (
    'curl -s -m 30 -X PUT -H @vast.header -H "Content-Type: application/json" '
    '-d "{\\"state\\":\\"stopped\\"}" "${CS_VAST_URL:-https://console.vast.ai}/api/v0/instances/$CONTAINER_ID/"'
)
ONSTART = "\n".join(
    [
        f"D={machine.REMOTE_DIR}; mkdir -p $D/logs; cd $D; umask 077;",
        "env | grep -E '^(CONTAINER_ID|CONTAINER_API_KEY|CS_VAST_URL)=' | sed 's/^/export /' > vast.env;",
        "printf 'Authorization: Bearer %s\\n' \"$CONTAINER_API_KEY\" > vast.header;",
        "date +%s > booted;",
        "nohup bash -c 'while sleep 60; do t=$(cat booted); "  # (in D: it starts where onstart is)
        'h=$(stat -c %Y heartbeat 2>/dev/null || echo 0); [ "$h" -gt "$t" ] && t=$h; '
        f"if [ $(( $(date +%s) - t )) -gt 2700 ]; then . ./vast.env; {_STOP_CALL}; fi; done' "
        "> logs/deadman.log 2>&1 &",
        "[ -f setup-done ] && nohup ./python cs.py watch >> logs/watch.log 2>&1 &",  # (the Python setup chose)
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
    offer_ids: list[int]  # the pick, then its backups
    disk_gb: float
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


@contextlib.contextmanager
def launch_log(out: Progress, name: str) -> Iterator[None]:
    """From here on, progress lines, and a failure (as `Launch failed: ...`), also go to NAME's launch log."""
    out.attach(name)
    try:
        yield
    except core.CrabError as e:
        out.line(f"Launch failed: {e.message}")
        raise


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
    chosen = available(a.offer_ids, a.disk_gb, cfg, out)
    if not chosen:
        raise core.CrabError(
            "none of the offers given can be rented any more: look at the offers again (crab-student offers) "
            "and launch with others.",
            4,
        )
    chosen, reason = priced(chosen, cfg["approve_above_usd_per_hour"])
    rec = new_record(a, cfg, chosen, repo, reason)
    p, backups = chosen[0], chosen[1:]
    out.line(f"Planning {a.name}")
    out.line(f"  machine  {offers.summary(p)} - ${p['dph_total']:.2f}/hr (offer {p['id']})")
    if backups:
        top = max(o["dph_total"] for o in backups)
        n = len(backups)
        out.line(
            f"  backups  {n} more offer{'s' if n > 1 else ''}, up to ${top:.2f}/hr (rented in order, each only if the "
            "machine before it does not start)"
        )
    out.line(f"  agent    {rec['mode']} - Claude Code: {settings_text(rec['claude_settings'])}")
    out.line(f"  repo     {repo_line}")
    out.line(f"  setup    {setup_text(a.setup)}")
    for w in warnings:
        out.line(f"Warning: {w}")
    if a.dry_run:
        return result(rec, "dry run", warnings, reason=reason)
    # (the lock is held to the end: no other command acts on the launch between its plan and its rent)
    with core.locked(a.name):
        save_new(rec, a.brief, warnings)
        with launch_log(out, a.name):
            if reason:
                return result(rec, "approval", warnings, reason=reason)
            show_launch(a.name, out)
            return bring_up(rec, out, warnings)


def check_new(a: LaunchArgs) -> None:
    core.check_name(a.name)
    check_offer_ids(a.offer_ids)
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


def check_offer_ids(ids: list[int]) -> None:
    if not 0 < len(ids) <= offers.MAX_OFFERS or len(set(ids)) < len(ids):
        raise core.CrabError(
            f"give 1 to {offers.MAX_OFFERS} different offers, the pick first: --offers ID,ID,ID", 2
        )


def available(ids: list[int], disk_gb: float, cfg: dict, out: Progress) -> list[dict]:
    """The offers among `ids` that can still be rented, in their order; each one gone is said. Two on one machine
    are refused: a fault of the host would sink both."""
    found = offers.named(ids, disk_gb, cfg)
    for i in ids:
        if i not in found:
            out.line(f"Offer {i} can no longer be rented.")
    chosen, machines = [found[i] for i in ids if i in found], {}
    for o in chosen:
        if (m := o.get("machine_id") or o["id"]) in machines:
            raise core.CrabError(
                f"offers {machines[m]} and {o['id']} are on the same machine: give backups on other machines.",
                2,
            )
        machines[m] = o["id"]
    return chosen


def priced(chosen: list[dict], cap: float) -> tuple[list[dict], str | None]:
    """The offers a launch may rent, and why it needs the user's approval first: no reason when the pick costs
    `cap` $/hr or less (backups over it are left out); else every offer stays, and the user approves the highest
    price among them."""
    pick, top = chosen[0]["dph_total"], max(o["dph_total"] for o in chosen)
    if pick <= cap:
        return [o for o in chosen if o["dph_total"] <= cap], None
    backups = f"; its backups cost up to ${top:.2f}/hr" if top > pick else ""
    return chosen, f"it costs more than ${cap:.2f}/hr (approve_above_usd_per_hour){backups}"


def plan_repo(a: LaunchArgs, cfg: dict, branch: str) -> tuple[dict, str, list[str]]:
    """Where the agent's branch starts, and its agent repo (`slug`; `source` is the user's repo). Nothing is written
    anywhere yet (the starting commit is a local, unreferenced object; a new agent repo is made after the rent)."""
    if a.no_repo:
        slug = github.agent_repo(None, cfg["workspace_repo"])
        new = "" if github.check_agent_repo(slug, None, branch) else ", new"
        repo = {
            "slug": slug,
            "source": None,
            "branch": branch,
            "start_sha": None,
            "branch_created": False,
            "dir": f"/root/work/{cfg['workspace_repo']}",
        }
        return repo, f"{slug} (your workspace repo{new}) - {branch}", []
    r = github.local_repo(a.repo_path or Path.cwd())
    slug = github.agent_repo(r.slug, cfg["workspace_repo"])
    new = "" if github.check_agent_repo(slug, r.slug, branch) else " (new, private)"
    sha, carried, untracked, local = github.snapshot(r, commit=not a.dry_run)
    base = (
        github.git(r.top, "symbolic-ref", "-q", "--short", "HEAD", check=False).strip() or "a detached HEAD"
    )
    line = f"{r.slug} - {branch} on {slug}{new} starts from {r.head[:7]} (your HEAD on {base})"
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
        "slug": slug,
        "source": r.slug,
        "branch": branch,
        "start_sha": sha,
        "local_dir": str(r.top),
        "carried": carried,
        "untracked": untracked,
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


def new_record(a: LaunchArgs, cfg: dict, chosen: list[dict], repo: dict, reason: str | None) -> dict:
    return {
        "name": a.name,
        "created": core.now_iso(),
        "phase": "pending" if reason else "renting",
        "error": None,
        "disk_gb": a.disk_gb,
        "max_dph": cfg["approve_above_usd_per_hour"],  # (the most it may rent at without asking again)
        "mode": a.mode or cfg["mode"],
        "claude_settings": claude_settings(a, cfg),
        "fake_claude": a.fake_claude,
        "setup": a.setup,
        "secrets": a.secrets,
        "session_id": str(uuid.uuid4()),
        "repo": repo,
        "offers": [offers.kept(o) for o in chosen],
        "failed_offers": [],
        **NO_MACHINE,
        "pending": {"reason": reason, "max_dph": max(o["dph_total"] for o in chosen)} if reason else None,
        "last_start": None,
        "stopped_since": None,
        "last_seen": None,
        "image": cfg["image"],
        "idle_minutes": cfg["idle_minutes"],
        "notify_url": cfg["notify_url"],
    }


def save_new(rec: dict, brief: str, warnings: list[str]) -> None:
    """The new agent's record, brief and launch log (its lock held)."""
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


def bring_up(rec: dict, out: Progress, warnings: list[str]) -> dict:
    """Bring a launch to ready, its lock held: a launch just planned (phase renting) rents its machine, one cut off
    during setup starts its machine again; then the upload and setup."""
    if rec["uploaded"]:
        resume_machine(rec, out)
    else:
        rent_machine(rec, out)
    warnings = warnings + setup_machine(rec, out)
    machine.write_ssh_config()
    out.line(f"Ready after {out.elapsed()}.")
    return result(rec, "working", warnings)


def continue_launch(rec: dict, out: Progress, offer_ids: list[int] | None) -> dict:
    """Continue a launch that was cut off or failed, its lock held. Cut off during setup, it goes on with its machine
    (the agent may be at work there). Any other starts again from its plan: its machine, which holds nothing, is
    destroyed, and its offers (or `offer_ids`) are looked up and priced again."""
    name, warnings = rec["name"], []
    if rec["phase"] == "ready":  # (the launch at work this waited for got there)
        return result(rec, "working", warnings)
    if rec["uploaded"]:
        if offer_ids:
            raise core.CrabError(
                f"{name} has its machine: --offers is only for a launch that failed or was cut off before its "
                "upload.",
                2,
            )
        return bring_up(rec, out, warnings)
    if rec["label"]:
        out.line("The launch was cut off before its upload: destroying its machine (it holds nothing).")
        warnings += drop_unuploaded(rec)
        for w in warnings:
            out.line(f"Warning: {w}")
    replan(rec, out, offer_ids)
    if rec["phase"] == "pending":
        return result(rec, "approval", warnings, reason=rec["pending"]["reason"])
    return bring_up(rec, out, warnings)


def resume_machine(rec: dict, out: Progress) -> None:
    """A launch cut off during setup: its machine is kept (the agent may already be at work), and may have
    stopped since; start it again and wait for it."""
    name, inst = rec["name"], vast.instance(rec["instance_id"])
    if inst is None:
        rec.update(NO_MACHINE, phase="failed", error="the machine no longer exists on Vast")
        core.save(rec)
        raise core.CrabError(
            f"{name}'s machine no longer exists on Vast. Try again: crab-student start {name}"
        )
    start_again(rec, inst, out)


def start_again(rec: dict, inst: dict, out: Progress) -> None:
    """Start the agent's machine again unless it runs (a new start: why it last stopped no longer holds), and wait
    until it answers ssh."""
    name = rec["name"]
    if not vast.running(inst):
        out.line(f"Starting {name}...")
        vast.set_state(rec["instance_id"], "running")
        rec["last_seen"] = {**(rec.get("last_seen") or {}), "stop_reason": None}
        rec.update(last_start=time.time(), stopped_since=None)
        core.save(rec)
    if why := wait_boot(rec, out, time.time() + BOOT_TIMEOUT):
        raise core.CrabError(
            f"{name} did not come back up ({why}). Vast keeps trying: check again with "
            f"crab-student status {name}, or stop it with crab-student stop {name}."
        )


def replan(rec: dict, out: Progress, offer_ids: list[int] | None) -> None:
    """A launch tried again: with the offers given, else with those of its offers that have not failed, if
    they can still be rented. Over the price it may rent at, it waits for approval."""
    cfg, name, failed = core.load_config(), rec["name"], {f["id"] for f in rec["failed_offers"]}
    ids = offer_ids or [o["id"] for o in rec["offers"] if o["id"] not in failed]
    chosen = available(ids, rec["disk_gb"], cfg, out)
    if not chosen:
        rec.update(phase="failed", error="none of its offers can be rented any more")
        core.save(rec)
        raise core.CrabError(
            f"none of {name}'s offers can be rented any more: look at the offers again (crab-student offers) "
            f"and give others: crab-student start {name} --offers ID,ID",
            4,
        )
    chosen, reason = priced(chosen, max(rec.get("max_dph") or 0, cfg["approve_above_usd_per_hour"]))
    pending = {"reason": reason, "max_dph": max(o["dph_total"] for o in chosen)} if reason else None
    rec.update(
        offers=[offers.kept(o) for o in chosen],
        failed_offers=[],
        phase="pending" if reason else "renting",
        error=None,
        pending=pending,
    )
    core.save(rec)
    if not reason:
        n = len(chosen)
        out.line(f"Trying {name} again with {n} offer{'s' if n > 1 else ''}.")


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:g} minute{'' if seconds == 60 else 's'}"


def rent_machine(rec: dict, out: Progress) -> None:
    """Rent the launch's offers one at a time, in their order, until a machine runs and answers ssh. One that does
    not is destroyed (nothing was uploaded to it) before the next offer is rented, so the launch never has two. While
    the first machine boots, the agent's branch goes on GitHub, in its agent repo."""
    while True:
        failed = {f["id"] for f in rec["failed_offers"]}
        offer = next((o for o in rec["offers"] if o["id"] not in failed), None)
        if offer is None:
            reasons = "; ".join(f"{f['id']}: {f['reason']}" for f in rec["failed_offers"][-3:])
            rec.update(phase="failed", error=f"no offer could be rented or started ({reasons})")
            core.save(rec)
            raise core.CrabError(
                f"every offer failed ({reasons}). Look at the offers again (crab-student offers) and give others: "
                f"crab-student start {rec['name']} --offers ID,ID",
                4,
            )
        if not rent(rec, offer, out):
            continue
        prepare_repo(rec, out)  # (while the machine boots; once done, it returns at once)
        why = boot(rec, out)
        if why is None:
            out.line(f"Offer {offer['id']} is ready after {out.elapsed()}.")
            return
        rec["failed_offers"].append({"id": offer["id"], "reason": why})
        drop_unuploaded(rec)
        out.line(f"Offer {offer['id']} {why}: destroyed it.")


def rent(rec: dict, offer: dict, out: Progress) -> bool:
    """Rent the offer as the launch's machine, the rent's label saved before the request; False when Vast refuses
    it. A rent whose outcome is unknown is never sent again: its machine is looked for by that label, and the launch
    fails if it has not shown up within LABEL_WAIT (no other machine is rented while it might exist)."""
    name, storage = rec["name"], offer.get("storage_cost")
    out.line(f"Renting offer {offer['id']} ({offer['short']}, ${offer['dph_total']:.2f}/hr).")
    machine.forget_host_key(name)  # (a new machine for this name has a new host key)
    rec.update(
        label=rent_label(rec),
        dph=offer["dph_total"],
        disk_usd_day=round(storage * rec["disk_gb"] / 30, 3) if storage else None,
        machine=offer["summary"],
        short=offer["short"],
        last_start=time.time(),
    )
    core.save(rec)  # (the label is saved before the request)
    try:
        rec["instance_id"] = vast.rent(offer["id"], rent_body(rec))
    except vast.VastError as e:
        rec.update(NO_MACHINE)
        if e.status == 402 or NO_CREDIT.search(e.message):
            rec.update(phase="failed", error=e.message)
            core.save(rec)
            raise core.CrabError(
                "your Vast account has no credit left. Add credit at https://cloud.vast.ai/billing/, then: "
                f"crab-student start {name}"
            ) from None
        rec["failed_offers"].append({"id": offer["id"], "reason": e.message})
        core.save(rec)
        out.line(f"  offer {offer['id']} is not available ({e.message}).")
        return False
    except vast.RentUnknown as e:
        out.line(f"  Vast did not confirm the rent ({e}); looking for its machine by its label.")
        rec["instance_id"] = find_rent(rec["label"])
        if not rec["instance_id"]:
            label = rec["label"]
            message = (
                f"Vast did not confirm the rent of offer {offer['id']}, and no machine labelled {label} has "
                f"appeared. Try again: crab-student start {name} (if a machine labelled {label} shows up later, "
                "crab-student list points it out: destroy it at https://cloud.vast.ai/instances/)."
            )
            rec.update(NO_MACHINE, phase="failed", error=message)
            core.save(rec)
            raise core.CrabError(message) from None
    core.save(rec)
    out.line(f"  offer {offer['id']}: instance {rec['instance_id']}")
    return True


def find_rent(label: str) -> int | None:
    """The machine with this rent label, looked for until LABEL_WAIT has passed (Vast out of reach meanwhile is
    waited out)."""
    deadline = time.monotonic() + LABEL_WAIT
    while True:
        with contextlib.suppress(vast.VastDown):
            if iid := find_by_label(label):
                return iid
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL)


def boot(rec: dict, out: Progress) -> str | None:
    """Wait until the launch's new machine runs and answers ssh: None, else why to give it up. It is given up when
    it fails, when Vast does not report it running START_TIMEOUT after its rent, or when its ssh does not answer
    SSH_TIMEOUT after it runs; Vast out of reach meanwhile is waited out."""
    running_at, errors, keyed, last = None, 0, False, "waiting"
    while True:
        try:
            inst = vast.instance(rec["instance_id"])
            if inst is None:
                return "disappeared"
            actual = inst.get("actual_status") or "waiting"
            msg = " ".join(str(inst.get("status_msg") or "").split())
            last = f"{actual}{' - ' + msg if msg else ''}"
            out.status(f"Booting: {actual}{' - ' + msg[:80] if msg else ''}")
            if actual in ("exited", "offline"):
                return f"failed to start (status {actual}{': ' + msg if msg else ''})"
            # (an error in the status message of a machine not running yet, seen twice in a row)
            errors = errors + 1 if actual != "running" and re.search(r"\b(error|failed)\b", msg, re.I) else 0
            if errors >= 2:
                return f"failed to start ({msg})"
            if actual == "running":
                running_at = running_at or time.time()
                if machine.set_address(rec, inst):
                    core.save(rec)
                    machine.write_ssh_config()
                if vast.address(inst):
                    if not keyed:
                        vast.add_ssh_key(rec["instance_id"], (core.ssh_dir() / "id_ed25519.pub").read_text())
                        keyed = True
                    if machine.reachable(rec["name"]):
                        return None
        except vast.VastDown as e:
            out.status(f"Booting: Vast does not answer - {e.message[:80]}")
        if running_at and time.time() > running_at + SSH_TIMEOUT:
            return f"did not answer ssh within {_minutes(SSH_TIMEOUT)} of starting"
        if not running_at and time.time() > rec["last_start"] + START_TIMEOUT:
            return f"did not start within {_minutes(START_TIMEOUT)} (last status: {last})"
        time.sleep(POLL)


def unconfirmed(label: str) -> str:
    """The warning about a rent Vast did not confirm whose machine was not found."""
    return (
        f"Vast did not confirm the rent labelled {label}; if a machine with that label shows up, crab-student list "
        "points it out (destroy it at https://cloud.vast.ai/instances/)."
    )


def rent_label(rec: dict) -> str:
    """A new rent's label: OWNER-NAME-<install id>-<4 hex> (OWNER: the `owner` setting, else crab-student)."""
    owner = core.load_config()["owner"] or "crab-student"
    return f"{owner}-{rec['name']}-{core.install_id()}-{secrets.token_hex(2)}"


def own_label(label: str) -> bool:
    """Whether a label is one of this computer's rents, whatever the owner was (teammates sharing the Vast account
    have another install id)."""
    return bool(re.search(rf"-{core.install_id()}-[0-9a-f]{{4}}$", label))


def find_by_label(label: str) -> int | None:
    return next((i["id"] for i in vast.instances() if i.get("label") == label), None)


def rent_body(rec: dict) -> dict:
    return {
        **RENT_FIXED,
        "image": rec["image"],
        "disk": float(rec["disk_gb"]),
        "label": rec["label"],
        "onstart": ONSTART,
        "env": {"CS_VAST_URL": vast.base_url()},
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
    """Destroy the launch's machine, which received nothing (found by its rent's label when Vast did not confirm
    the rent), and forget it. The warning when Vast did not confirm the rent and no machine has its label."""
    iid = rec["instance_id"] or (rec["label"] and find_by_label(rec["label"]))
    warnings = [unconfirmed(rec["label"])] if rec["label"] and not iid else []
    if iid:
        vast.destroy(iid)
    rec.update(NO_MACHINE)
    core.save(rec)
    machine.write_ssh_config()
    return warnings


def prepare_repo(rec: dict, out: Progress) -> None:
    """The agent repo and its deploy key (see github.prepare_agent_repo), and the agent's branch there (once; the
    record says what is done)."""
    repo, name = rec["repo"], rec["name"]
    if repo["branch_created"]:
        return
    try:
        key = github.prepare_agent_repo(repo["slug"], repo["source"])
        if not repo["start_sha"]:  # (the workspace: the branch starts from its default branch)
            repo["start_sha"] = github.default_head(repo["slug"])
        github.create_branch(repo, key)
    except core.CrabError as e:
        warnings = drop_unuploaded(rec)
        rec.update(phase="failed", error=e.message)
        core.save(rec)
        message = (
            f"{e.message} (the machine rented for it was destroyed: it held nothing). Fix that, then: "
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
        "deploy_key": (github.key_file(rec["repo"]["slug"]).read_bytes().replace(b"\r\n", b"\n"), 0o600),
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
        rec.update(NO_MACHINE, phase="failed", error=e.message)
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
    """Launch an agent waiting for approval, with those of its offers still to be had at up to the price the user
    approved; one whose offers all cost more by now waits again."""
    with core.locked(name):
        rec = core.load(name)
        pending = rec.get("pending")
        if not pending:
            raise core.CrabError(f"{name} is not waiting for approval.")
        repo = rec["repo"]
        if not repo["branch_created"]:  # (a new launch: are the checks still true?)
            github.check_agent_repo(repo["slug"], repo["source"], repo["branch"])
        chosen = available([o["id"] for o in rec["offers"]], rec["disk_gb"], core.load_config(), out)
        if not chosen:
            raise core.CrabError(
                f"the machines {name} would rent can no longer be rented: drop it (crab-student decline {name}) "
                "and launch it again.",
                4,
            )
        within = [o for o in chosen if o["dph_total"] <= pending["max_dph"]]
        if not within:  # (prices went up while it waited: ask again)
            reason = f"it costs more than the ${pending['max_dph']:.2f}/hr you approved"
            rec.update(
                offers=[offers.kept(o) for o in chosen],
                pending={"reason": reason, "max_dph": max(o["dph_total"] for o in chosen)},
            )
            core.save(rec)
            return result(rec, "approval", rec.get("warnings", []), reason=reason)
        rec.update(
            offers=[offers.kept(o) for o in within],
            failed_offers=[],
            phase="renting",
            pending=None,
            max_dph=pending["max_dph"],
        )
        core.save(rec)
        with launch_log(out, name):
            show_launch(name, out)
            return bring_up(rec, out, rec.get("warnings", []))


def decline(name: str) -> str:
    with core.locked(name):
        if not core.load(name).get("pending"):
            raise core.CrabError(f"{name} is not waiting for approval.")
        core.remove(name)
    return f"Declined {name}: nothing was rented."


def start(name: str, out: Progress, offer_ids: list[int] | None = None) -> dict:
    """Start a stopped agent again, or continue a launch that was interrupted or failed (on `offer_ids`, when
    given, instead of its own offers)."""
    rec = core.load(name)
    if rec["phase"] == "pending":
        raise core.CrabError(f"{name} is waiting for approval: crab-student approve {name}", 3)
    if offer_ids is not None:
        check_offer_ids(offer_ids)
    if rec["phase"] != "ready":
        show_launch(name, out)
        # (a second caller waits for a launch at work, then goes on from where it got)
        with core.locked(name, wait=900), launch_log(out, name):
            return continue_launch(core.load(name), out, offer_ids)
    if offer_ids:
        raise core.CrabError(
            f"{name} is set up: --offers is only for a launch that failed or was cut off.", 2
        )
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
        start_again(rec, inst, out)
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
        "repo": rec["repo"]["source"] or rec["repo"]["slug"],
        "branch": branch,
        "exit_code": 3 if state == "approval" else 0,
    }
