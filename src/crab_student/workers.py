"""Workers once launched: the views (list, status), stop, delete, send and report."""

from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from crab_student import core, github, launch, machine, vast

STOP_REASONS = {"user": "stopped by you", "idle": "idle stop", "time-limit": "time limit"}


@dataclass
class View:
    name: str
    state: str
    message: str
    used_h: float | None
    spent: float | None
    budget: float | None
    limit_h: float | None
    idle_min: float | None
    machine: str
    dph: float | None
    branch: str
    repo: str
    git: dict | None
    vast_status: str | None
    pending: dict | None
    error: str | None


def _status(rec: dict) -> dict | None:
    try:
        return machine.remote(rec["name"], ["status"], timeout=20)
    except core.CrabError:
        return None


def views(names: list[str] | None = None) -> list[View]:
    """Every worker (or those named) as it is now: one Vast call, and one ssh call per running machine."""
    recs = [r for r in core.all_records() if names is None or r["name"] in names]
    insts: dict | None = {}
    if any(r.get("instance_id") for r in recs):
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
        busy = core.is_locked(rec["name"])
        if not busy and (inst or st):
            moved |= _remember(rec, inst, st)
        state, message = display_state(rec, inst, st, busy, insts is None)
        result.append(_view(rec, inst, st, state, message))
    if moved:
        machine.write_ssh_config()
    return result


def _remember(rec: dict, inst: dict | None, st: dict | None) -> bool:
    """Keep the last things seen (shown while the machine is stopped) and a changed address."""
    with contextlib.suppress(core.CrabError), core.locked(rec["name"]):
        fresh = core.load(rec["name"])
        moved = bool(inst) and machine.set_address(fresh, inst)
        if st:
            w = st.get("worker") or {}
            fresh["last_seen"] = {
                "at": core.now_iso(),
                "state": w.get("state"),
                "message": w.get("message"),
                "used_hours": st.get("used_hours"),
                "stop_reason": st.get("stop_reason"),
                "git": st.get("git"),
            }
        core.save(fresh)
        rec.update(last_seen=fresh["last_seen"], ssh=fresh["ssh"])
        return moved
    return False


def display_state(
    rec: dict, inst: dict | None, st: dict | None, busy: bool, vast_down: bool = False
) -> tuple[str, str]:
    """(STATE, MESSAGE) for list and status."""
    name, phase, last = rec["name"], rec["phase"], rec.get("last_seen") or {}
    if phase == "pending":
        return "approval", f"waiting for approval: {rec['pending']['reason']}"
    if phase in ("renting", "booting", "setup"):
        if busy:
            return "launching", {"renting": "renting a machine", "booting": "booting", "setup": "setting up"}[
                phase
            ]
        if phase == "renting" and rec.get("rent"):
            return "rent unknown", f"run crab-student start {name} to look for the machine"
        return "interrupted", f"launch interrupted: crab-student start {name} continues it"
    if phase == "failed":
        return "launch failed", rec.get("error") or ""
    if vast_down:
        return "unknown", "could not reach Vast; " + (last.get("message") or "")
    if inst is None:
        return "gone", "the machine no longer exists on Vast"
    pend = f" (waiting for approval to {rec['pending']['action']})" if rec.get("pending") else ""
    if not vast.running(inst):
        if (inst.get("intended_status") or inst.get("cur_state")) == "running":
            return "starting", (" ".join(str(inst.get("status_msg") or "").split()) or "starting") + pend
        why = STOP_REASONS.get(last.get("stop_reason") or "")
        return "stopped", " - ".join(x for x in (why, last.get("message")) if x) + pend
    if st is None:
        return "unreachable", (last.get("message") or "no answer over ssh") + pend
    if st.get("stopping"):
        return "stopping", STOP_REASONS.get(st.get("stop_reason") or "", "stopping")
    w, git = st.get("worker") or {}, st.get("git") or {}
    state, msg = w.get("state"), (w.get("message") or "") + pend
    if state == "needs-you":
        return "needs you", msg
    if state == "done":
        return (
            ("done*", msg + " (not all pushed)") if git.get("dirty") or git.get("unpushed") else ("done", msg)
        )
    if state == "failed":
        return "failed", msg
    if not state:
        return "starting", "worker starting" + pend
    idle = st.get("idle_seconds") or 0
    return ("idle", f"idle {idle // 60:.0f}m - {msg}") if idle >= 600 else ("working", msg)


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
        budget=rec.get("budget_usd"),
        limit_h=rec.get("limit_hours"),
        idle_min=round(st["idle_seconds"] / 60) if st else None,
        machine=rec.get("machine") or "",
        dph=dph,
        branch=rec["repo"]["branch"],
        repo=rec["repo"]["slug"],
        git=st.get("git") if st else last.get("git"),
        vast_status=inst.get("actual_status") if inst else None,
        pending=rec.get("pending"),
        error=rec.get("error"),
    )


def view(name: str) -> View:
    core.load(name)
    return views([name])[0]


# ---- actions -------------------------------------------------------------------------------------


def stop(name: str, out: launch.Progress) -> dict:
    """Save what the worker has, then stop the machine (the disk is kept)."""
    with core.locked(name):
        rec = core.load(name)
        if rec["phase"] == "pending":
            raise core.CrabError(
                f"{name} is waiting for approval and has no machine yet. To drop it: crab-student "
                f"decline {name}"
            )
        if rec["phase"] != "ready":
            raise core.CrabError(
                f"{name} has not finished launching, so there is nothing to stop. To give up on it: "
                f"crab-student delete {name}"
            )
        inst = vast.instance(rec["instance_id"])
        if inst is None:
            raise core.CrabError(
                f"{name}'s machine no longer exists on Vast. Delete the worker: crab-student delete {name}"
            )
        if not vast.wants_running(inst):
            return {"name": name, "result_line": f"{name} is already stopped."}
        if vast.running(inst):
            try:
                s = machine.remote(name, ["save", "--reason", "user"], timeout=200)
                out.line(
                    f"Saved: {s['committed']} change(s) committed, {'pushed' if s['pushed'] else 'NOT pushed'}"
                    + (f" ({s['error']})" if s.get("error") else "")
                    + "."
                )
            except core.CrabError as e:
                out.line(f"Could not save first ({e.message}); the disk is kept, so nothing is lost.")
        vast.set_state(rec["instance_id"], "stopped")
        rec["last_seen"] = {**(rec.get("last_seen") or {}), "stop_reason": "user", "at": core.now_iso()}
        core.save(rec)
    return {
        "name": name,
        "result_line": f"Stopped {name}. Its disk is kept (storage is still billed); start it "
        f"again with crab-student start {name}.",
    }


def _find_instance(rec: dict) -> dict | None:
    if rec.get("instance_id"):
        return vast.instance(rec["instance_id"])
    if rec["phase"] == "renting" and rec.get("rent"):
        iid = launch.find_by_label(rec["rent"]["label"], 0)
        return vast.instance(iid) if iid else None
    return None


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
    """Destroy the machine and forget the worker; refuses (exit 3) when that would lose work, unless forced."""
    with core.locked(name):
        rec = core.load(name)
        if rec["phase"] == "pending":
            core.remove(name)
            return {
                "name": name,
                "result_line": f"Deleted {name} (it was waiting for approval; nothing was rented).",
            }
        inst = _find_instance(rec)
        if inst and rec["phase"] == "ready" and not force:  # (only a finished launch can hold work)
            if not vast.running(inst):
                raise core.CrabError(
                    f"{name} is stopped, so crab-student cannot check it for unsaved work. Start it "
                    f"(crab-student start {name}) and delete again, or delete anyway with --force.",
                    3,
                )
            try:
                res = machine.remote(name, ["check"], timeout=240)
            except core.CrabError as e:
                raise core.CrabError(
                    f"could not check {name} for unsaved work ({e.message}). Try again, or delete "
                    "anyway with --force.",
                    3,
                ) from None
            if problems := _problems(res):
                cfg = str(machine.config_file()).replace("\\", "/")
                raise core.CrabError(
                    "\n".join(
                        [
                            f"{name} has work that would be lost:",
                            *(f"  - {p}" for p in problems),
                            "Copy what you need first, for example:",
                            f"  scp -F {cfg} -r {machine.alias(name)}:/root/outputs ./{name}-outputs",
                            f"Then delete again, or delete anyway with: crab-student delete {name} --force",
                        ]
                    ),
                    3,
                )
        if inst:
            vast.destroy(inst["id"])
            out.line(f"Destroyed machine {inst['id']}.")
        lines = [f"Deleted {name}.", *_cleanup(rec)]
    return {"name": name, "result_line": "\n".join(lines)}


def _cleanup(rec: dict) -> list[str]:
    """Remove what the worker had on GitHub and locally; its branch stays unless nobody worked on it."""
    repo, notes = rec["repo"], []
    try:
        if repo.get("deploy_key_id"):
            github.remove_deploy_key(repo["slug"], repo["deploy_key_id"])
        if repo.get("branch_created") and github.delete_branch_if_at(
            repo["slug"], repo["branch"], repo["start_sha"]
        ):
            notes.append(f"Branch {repo['branch']} was removed (nothing had been done on it).")
        elif repo.get("branch_created"):
            notes.append(f"Branch {repo['branch']} stays on {repo['slug']}.")
    except core.CrabError as e:
        notes.append(
            f"Warning: could not tidy up on GitHub ({e.message}). Remove the deploy key 'crab-student "
            f"{rec['name']}' from {repo['slug']} by hand."
        )
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
    machine.remote(name, ["send"], input=text.encode("utf-8"))
    return {"name": name, "result_line": f"Sent to {name}. It reads messages between its steps."}


def report(name: str) -> str:
    rec = core.load(name)
    path = f"agent-runs/{name}/REPORT.md"
    text = github.read_report(rec["repo"]["slug"], rec["repo"]["branch"], path)
    if text is None:
        raise core.CrabError(
            f"there is no {path} on {rec['repo']['branch']} yet. The worker writes it as it goes."
        )
    return text
