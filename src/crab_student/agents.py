"""Agents once launched: the views (list, status), stop, delete, send, report and open."""

from __future__ import annotations

import contextlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from crab_student import core, github, launch, machine, vast

STOP_REASONS = {"user": "stopped by you", "idle": "idle stop", "done": "finished"}
IDLE_SHOWN = 600  # seconds without work or conversation before a running agent is shown as idle
STOP_STUCK = 300  # a machine still running this long after asking Vast to stop it gets stopped from here
NOTE_MARGIN = 300  # a stop note must be this much newer than the last start (the clocks may differ a little)
FOLLOW_POLL = 1.0  # how often open looks at a launch it follows
QUIET_ENDS = 3  # looks in a row without the launch at work (its lock free) before open says it stopped


@dataclass
class View:
    name: str
    state: str
    message: str
    used_h: float | None
    spent: float | None
    idle_min: float | None
    machine: str
    dph: float | None
    branch: str
    repo: str
    git: dict | None
    vast_status: str | None
    pending: dict | None
    error: str | None
    stopped_h: float | None  # how long a stopped machine has been sitting (its disk is still billed)
    disk_usd_day: float | None
    mode: str
    frontend: str | None  # where the agent's Claude runs: "tmux", or "vscode" (interactive, not handed off)
    settings: dict  # the Claude Code settings the machine was given (model, effortLevel, fastMode)


def _status(rec: dict) -> dict | None:
    try:
        return machine.remote(rec["name"], ["status"], timeout=20)
    except core.CrabError:
        return None


def views(names: list[str] | None = None) -> list[View]:
    return listing(names)[0]


def listing(names: list[str] | None = None) -> tuple[list[View], list[dict]]:
    """Every agent (or those named) as it is now, and the machines that belong to no agent (see orphans): one
    Vast call, and one ssh call per running machine."""
    recs = [r for r in core.all_records() if names is None or r["name"] in names]
    insts: dict | None = {}
    if core.vast_key_file().exists() or any(r.get("instance_id") for r in recs):
        try:
            insts = {i["id"]: i for i in vast.instances()}
        except core.CrabError:
            insts = None  # (Vast unreachable: show what is known)
    live = [r for r in recs if r["phase"] == "ready" and insts and vast.running(insts.get(r["instance_id"]))]
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = dict(zip([r["name"] for r in live], pool.map(_status, live), strict=True))
    result, moved = [], False
    for rec in recs:
        inst = insts.get(rec.get("instance_id")) if insts else None
        st = statuses.get(rec["name"])
        if st and (st.get("stop_sent_for") or 0) > STOP_STUCK:
            vast.set_state(rec["instance_id"], "stopped")  # (Vast did not take the machine's own stop call)
        busy = core.is_locked(rec["name"])
        if not busy and (inst or st):
            moved |= _remember(rec, inst, st)
        state, message = display_state(rec, inst, st, busy, insts is None)
        result.append(_view(rec, inst, st, state, message))
    if moved:
        machine.write_ssh_config()
    # (records read again: a launch running alongside may have saved a rent since the first read)
    return result, orphans(list((insts or {}).values()), core.all_records())


def orphans(insts: list[dict], recs: list[dict]) -> list[dict]:
    """The instances labelled as rented by crab-student on this computer that are no agent's (its machine, or one
    of its rents): most likely a rent Vast did not confirm that went through after all. Teammates' machines on a
    shared Vast account carry another install id, so they never show. Never destroyed from here."""
    labels = {r["label"] for rec in recs for r in [rec.get("rent"), *(rec.get("rents") or [])] if r}
    ids = {rec.get("instance_id") for rec in recs}
    return [
        i
        for i in insts
        if launch.own_label(str(i.get("label") or "")) and i.get("label") not in labels and i["id"] not in ids
    ]


def _remember(rec: dict, inst: dict | None, st: dict | None) -> bool:
    """Keep the last things seen (shown while the machine is stopped), since when it is stopped, and a changed
    address."""
    with contextlib.suppress(core.CrabError), core.locked(rec["name"]):
        fresh = core.load(rec["name"])
        moved = bool(inst) and machine.set_address(fresh, inst)
        if inst:
            up = vast.wants_running(inst)
            if up and fresh.get(
                "stopped_since"
            ):  # (started again, maybe not by crab-student: old notes are stale)
                fresh["last_start"] = time.time()
            fresh["stopped_since"] = None if up else fresh.get("stopped_since") or time.time()
        if st:
            a = st.get("agent") or {}
            fresh["last_seen"] = {
                "state": a.get("state"),
                "message": a.get("message"),
                "used_hours": st.get("used_hours"),
                "stop_reason": st.get("stop_reason"),
                "git": st.get("git"),
                "frontend": st.get("frontend"),
            }
            if st.get("mode"):  # (a hand-off changes the machine's mode)
                fresh["mode"] = rec["mode"] = st["mode"]
        core.save(fresh)
        rec.update(last_seen=fresh["last_seen"], ssh=fresh["ssh"], stopped_since=fresh.get("stopped_since"))
        return moved
    return False


def display_state(
    rec: dict, inst: dict | None, st: dict | None, busy: bool, vast_down: bool = False
) -> tuple[str, str]:
    """(STATE, MESSAGE) for list and status. The states: approval, starting, interrupted, working, needs you,
    interactive, done, failed, stopped, unknown."""
    name, phase, last = rec["name"], rec["phase"], rec.get("last_seen") or {}
    if phase == "pending":
        return "approval", f"waiting for your approval: {rec['pending']['reason']}"
    if phase in ("renting", "booting", "setup"):
        if busy:
            return "starting", {"renting": "renting a machine", "booting": "booting", "setup": "setting up"}[
                phase
            ]
        return "interrupted", f"launch interrupted: crab-student start {name} tries again"
    if phase == "failed":
        return "failed", rec.get("error") or ""
    if vast_down:
        return "unknown", "could not reach Vast; " + (last.get("message") or "")
    if inst is None:
        return "failed", f"the machine no longer exists on Vast: crab-student delete {name}"
    if not vast.running(inst):
        if vast.wants_running(inst):
            return "starting", " ".join(str(inst.get("status_msg") or "").split()) or "starting"
        why = STOP_REASONS.get(last.get("stop_reason") or "") or _stopped_itself(last)
        return "stopped", " - ".join(x for x in (why, _stopped_for(rec), last.get("message")) if x)
    if st is None:
        return "unknown", last.get("message") or "no answer over ssh"
    if st.get("stopping"):
        return "stopped", "stopping: " + STOP_REASONS.get(st.get("stop_reason") or "", "")
    idle = st.get("idle_seconds") or 0
    if st.get("frontend") == "vscode":
        return (
            "interactive",
            f"in VS Code, idle {idle // 60:.0f}m" if idle >= IDLE_SHOWN else "in VS Code, active",
        )
    a, git = st.get("agent") or {}, st.get("git") or {}
    state, msg = a.get("state"), a.get("message") or ""
    if state == "needs-you":
        return "needs you", msg
    if state == "done":
        return "done", msg + (" (not all pushed)" if git.get("dirty") or git.get("unpushed") else "")
    if state == "failed":
        return "failed", msg
    if not state:
        return "starting", "agent starting"
    return "working", f"idle {idle // 60:.0f}m - {msg}" if idle >= IDLE_SHOWN else msg


def _stopped_for(rec: dict) -> str:
    since = rec.get("stopped_since")
    if not since:
        return ""
    disk = f", disk ~${rec['disk_usd_day']:.2f}/day" if rec.get("disk_usd_day") else ""
    return f"{core.fmt_hours((time.time() - since) / 3600)} ago{disk}"


def _stopped_itself(last: dict) -> str:
    """The machine stopped and the user did not do it: it stopped itself (idle or finished), as far as can be
    told from here (the watchdog's own reason stays on the stopped machine)."""
    return "" if last.get("used_hours") is None else "stopped itself"


def _view(rec: dict, inst: dict | None, st: dict | None, state: str, message: str) -> View:
    last = rec.get("last_seen") or {}
    used = st.get("used_hours") if st else last.get("used_hours")
    dph = rec.get("dph")
    return View(
        name=rec["name"],
        state=state,
        message=core.ascii_text(" ".join(message.split())),
        used_h=used,
        spent=round(used * dph, 2) if used is not None and dph else None,
        idle_min=round(st["idle_seconds"] / 60) if st else None,
        machine=rec.get("machine") or "",
        dph=dph,
        branch=rec["repo"]["branch"],
        repo=rec["repo"]["slug"],
        git=st.get("git") if st else last.get("git"),
        vast_status=inst.get("actual_status") if inst else None,
        pending=rec.get("pending"),
        error=rec.get("error"),
        stopped_h=(time.time() - rec["stopped_since"]) / 3600
        if state == "stopped" and rec.get("stopped_since")
        else None,
        disk_usd_day=rec.get("disk_usd_day"),
        mode=rec.get("mode") or "",
        frontend=st.get("frontend") if st else last.get("frontend"),
        settings=rec.get("claude_settings") or {},
    )


def view(name: str) -> View:
    core.load(name)
    return views([name])[0]


# ---- actions -------------------------------------------------------------------------------------


def stop(name: str, out: launch.Progress) -> dict:
    """Save what the agent has, then stop the machine (the disk is kept). The final save pushes the stop note
    that lets delete check the stopped machine without starting it."""
    with core.locked(name):
        rec = core.load(name)
        if rec["phase"] == "pending":
            raise core.CrabError(
                f"{name} is waiting for approval and has no machine yet. To drop it: crab-student decline {name}"
            )
        if not (rec.get("uploaded") and rec.get("instance_id")):
            raise core.CrabError(
                f"{name} has not finished launching, so there is nothing to stop. To give up on it: "
                f"crab-student delete {name}"
            )
        inst = vast.instance(rec["instance_id"])
        if inst is None:
            raise core.CrabError(
                f"{name}'s machine no longer exists on Vast. Delete the agent: crab-student delete {name}"
            )
        if not vast.wants_running(inst):
            return {"name": name, "result_line": f"{name} is already stopped."}
        if vast.running(inst):
            try:
                s = machine.remote(name, ["save", "--reason", "user", "--final"], timeout=200)
                out.line(
                    f"Saved: {s['committed']} change(s) committed, {'pushed' if s['pushed'] else 'NOT pushed'}"
                    + (f" ({s['error']})" if s.get("error") else "")
                    + "."
                )
            except core.CrabError as e:
                out.line(f"Could not save first ({e.message}); the disk is kept, so nothing is lost.")
        vast.set_state(rec["instance_id"], "stopped")
        rec["last_seen"] = {**(rec.get("last_seen") or {}), "stop_reason": "user"}
        rec["stopped_since"] = time.time()
        core.save(rec)
    return {
        "name": name,
        "result_line": f"Stopped {name}. Its disk is kept (storage is still billed); start it "
        f"again with crab-student start {name}.",
    }


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    raise AssertionError


def _problems(res: dict) -> list[str]:
    problems = []
    if res.get("unpushed"):
        problems.append(f"{res['unpushed']} commit(s) not pushed to GitHub")
    if res.get("dirty"):
        problems.append(f"{res['dirty']} uncommitted file(s) in the repo")
    outputs = res.get("outputs") or {}
    if outputs.get("files"):
        sample = ", ".join(outputs.get("sample") or [])
        problems.append(
            f"/root/outputs: {outputs['files']} file(s), {_size(outputs.get('bytes') or 0)} ({sample})"
        )
    problems += [
        f"large file outside git: {f['path']} ({_size(f['bytes'])})" for f in res.get("large_files") or []
    ]
    return problems


def delete(name: str, force: bool, out: launch.Progress) -> dict:
    """Destroy the machine and forget the agent; refuses (exit 3) when that would lose work, unless forced."""
    with core.locked(name):
        rec = core.load(name)
        if rec["phase"] == "pending":
            core.remove(name)
            return {
                "name": name,
                "result_line": f"Deleted {name} (it was waiting for approval; nothing was rented).",
            }
        inst = vast.instance(rec["instance_id"]) if rec.get("instance_id") else None
        if inst and rec.get("uploaded") and not force:  # (a machine that received its files may hold work)
            running = vast.running(inst)
            if running:
                try:
                    res = machine.remote(name, ["check"], timeout=240)
                except core.CrabError as e:
                    raise core.CrabError(
                        f"could not check {name} for unsaved work ({e.message}). Try again, or delete "
                        "anyway with --force.",
                        3,
                    ) from None
            elif (note := _stop_note(rec)) is not None:
                res = {"dirty": note.get("not_committed_count", 0), "outputs": note.get("outputs"),
                       "large_files": note.get("large_files")}  # fmt: skip
            else:
                raise core.CrabError(
                    f"{name} is stopped, and crab-student does not know what it left on the machine. Start it "
                    f"(crab-student start {name}) and delete again, or delete anyway with --force.",
                    3,
                )
            if problems := _problems(res):
                cfg = str(machine.config_file()).replace("\\", "/")
                start = [] if running else [f"  crab-student start {name}"]
                raise core.CrabError(
                    "\n".join(
                        [
                            f"{name} has work that would be lost:",
                            *(f"  - {p}" for p in problems),
                            "Copy what you need first, for example:",
                            *start,
                            f"  scp -F {cfg} -r {machine.alias(name)}:/root/outputs ./{name}-outputs",
                            f"Then delete again, or delete anyway with: crab-student delete {name} --force",
                        ]
                    ),
                    3,
                )
        ids, lost = [inst["id"]] if inst else [], []
        for r in rec["rents"]:  # (a race cut off: its machines never received anything)
            if iid := r["instance_id"] or launch.find_by_label(r["label"]):
                ids.append(iid)
            else:
                lost.append(f"Warning: {launch.unconfirmed(r)}")
        for iid in ids:
            vast.destroy(iid)
            out.line(f"Destroyed machine {iid}.")
        lines = [f"Deleted {name}.", *_cleanup(rec), *lost]
    return {"name": name, "result_line": "\n".join(lines)}


def _stop_note(rec: dict) -> dict | None:
    """What a stopped machine left behind at its latest stop: the note its final save (the watchdog's, or
    `stop`'s) pushed to the branch as agent-runs/<name>/stopped.json. It counts only if written after the last
    start seen (a stop that could not push its note leaves an older one). None when there is no such note, or
    GitHub cannot be asked."""
    repo = rec["repo"]
    try:
        text = github.read_file(repo["slug"], repo["branch"], f"agent-runs/{rec['name']}/stopped.json")
        note = json.loads(text) if text else None
    except (core.CrabError, ValueError):
        return None
    stopped = note.get("stopped_at") if isinstance(note, dict) else None
    if isinstance(stopped, int | float) and stopped >= (rec.get("last_start") or 0) + NOTE_MARGIN:
        return note
    return None


def _cleanup(rec: dict) -> list[str]:
    """Remove what the agent had on GitHub and locally: its deploy key unless another agent shares it, and
    its branch unless somebody worked on it."""
    repo, notes, key = rec["repo"], [], rec["repo"].get("deploy_key")
    try:
        github.release_deploy_key(rec)
    except core.CrabError as e:
        notes.append(
            f"Warning: could not remove the deploy key '{github.key_title(key)}' from {repo['slug']} "
            f"({e.message}). Remove it by hand: the repo's Settings > Deploy keys."
        )
    try:
        if repo.get("branch_created") and github.delete_branch_if_at(
            repo["slug"], repo["branch"], repo["start_sha"]
        ):
            notes.append(f"Branch {repo['branch']} was removed (nothing had been done on it).")
        elif repo.get("branch_created"):
            notes.append(f"Branch {repo['branch']} stays on {repo['slug']}.")
    except core.CrabError as e:
        notes.append(f"Warning: could not check {repo['branch']} on GitHub ({e.message}); it stays there.")
    machine.forget_host_key(rec["name"])
    core.remove(rec["name"])
    machine.write_ssh_config()
    return notes


def send(name: str, text: str) -> dict:
    if not text.strip():
        raise core.CrabError("the message is empty.", 2)
    rec = core.load(name)
    if rec["phase"] != "ready" or not vast.running(vast.instance(rec["instance_id"])):
        raise core.CrabError(
            f"{name} is not running, so it cannot get a message. Start it: crab-student start {name}"
        )
    if (_status(rec) or {}).get("frontend") == "vscode":
        raise core.CrabError(
            f"{name} is an interactive agent in VS Code: type to it there. {_vscode_hint(name)}"
        )
    machine.remote(name, ["send"], input=text.encode("utf-8"))
    return {"name": name, "result_line": f"Sent to {name}. It reads messages between its steps."}


def report(name: str) -> str:
    rec = core.load(name)
    path = f"agent-runs/{name}/REPORT.md"
    text = github.read_file(rec["repo"]["slug"], rec["repo"]["branch"], path)
    if text is None:
        why = (
            "An interactive agent writes no report"
            if rec.get("mode") == "interactive"
            else "The agent writes it as it goes"
        )
        raise core.CrabError(f"there is no {path} on {rec['repo']['branch']} yet. {why}.")
    return text


# ---- terminals -----------------------------------------------------------------------------------


def _vscode_hint(name: str) -> str:
    return f"Open it in VS Code: crab-student open {name} --vscode"


def open_one(name: str) -> int:
    """Attach the terminal to a running agent. An interactive agent whose Claude works in VS Code is opened there,
    and on a terminal this then waits for its hand-off and attaches to the agent in tmux."""
    rec = core.load(name)
    status = machine.agent_status(rec)
    if status["frontend"] != "vscode":
        return machine.open_one(name)
    machine.open_vscode(rec, status)
    if not machine.interactive() or not machine.wait_for_handoff(name):
        return 0
    return machine.open_one(name)


def open_all(names: list[str]) -> int:
    """Every running agent side by side, except interactive agents in VS Code (they have no terminal session)."""
    shown = views(names)
    skipped = [v.name for v in shown if v.frontend == "vscode"]
    rest = [v.name for v in shown if v.frontend != "vscode"]
    if skipped:
        print(f"Skipped {', '.join(skipped)}: interactive, in VS Code (crab-student open NAME --vscode).")
    if not rest:
        raise core.CrabError(
            "every running agent is interactive and in VS Code: crab-student open NAME --vscode"
        )
    return machine.open_all(rest)


def follow_launch(name: str) -> bool:
    """Show an agent's launch as it happens: its log so far, then each new line, until it ends. True once the agent
    is ready; else False, after saying what happened and what to do."""
    print(
        f"Launching {name}. Closing this window does not stop the launch (see it again: crab-student open {name})."
    )
    shown, quiet = _LogView(name), 0
    while True:
        shown.update()
        if not core.record_path(name).exists():
            print(f"{name} was deleted.")
            return False
        busy = core.is_locked(name)
        quiet = 0 if busy else quiet + 1
        rec = core.load(name)
        if not busy and (rec["phase"] in ("ready", "failed", "pending") or quiet >= QUIET_ENDS):
            shown.update()
            print(core.ascii_text(_launch_outcome(rec)), flush=True)
            return rec["phase"] == "ready"
        time.sleep(FOLLOW_POLL)


def _launch_outcome(rec: dict) -> str:
    name, phase = rec["name"], rec["phase"]
    if phase == "ready":
        return f"{name} is ready."
    if phase == "failed":
        return (
            f"The launch of {name} failed: {rec.get('error') or 'see above'}. Try again: crab-student start {name} "
            f"(or give up: crab-student delete {name})."
        )
    if phase == "pending":
        return (
            f"{name} is waiting for your approval: {rec['pending']['reason']}. Approve it: crab-student approve "
            f"{name} (or: crab-student decline {name})."
        )
    return f"The launch of {name} stopped before it finished. Try again: crab-student start {name}"


class _LogView:
    """The new lines of a launch log, printed as they come (a status line without its "~" marker)."""

    def __init__(self, name: str):
        self.path, self.pos = launch.log_file(name), 0

    def update(self) -> None:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return
        complete = data[: data.rfind(b"\n") + 1]  # (a line still being written waits for the next look)
        self.pos += len(complete)
        for line in complete.decode("utf-8", "replace").splitlines():
            clock, _, text = line.partition(" ")
            print(core.ascii_text(f"{clock} {text.removeprefix('~ ')}"), flush=True)
