"""Errors, paths, settings, agent records and their locks, secrets, subprocesses, formatting."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import keyring
import keyring.errors


class CrabError(Exception):
    """A failure the user can act on. `code`: 1 error, 2 usage, 3 needs approval / refused by a limit /
    would lose work, 4 no machine fits, 5 setup failed."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.message = message
        self.code = code


# ---- paths ---------------------------------------------------------------------------------------


def home() -> Path:
    return Path.home() / ".crab-student"


def ssh_path(path: Path) -> str:
    """A path for an ssh config line that both Windows' OpenSSH and Git's ssh understand: "~/..." under the home
    folder (Git's ssh reads "C:/..." as a relative path, and VS Code may run it), else quoted with "/"."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return f'"{path.as_posix()}"'


def agents_dir() -> Path:
    return home() / "agents"


def ssh_dir() -> Path:
    return home() / "ssh"


def keys_dir() -> Path:
    return home() / "keys"


def install_id() -> str:
    """This computer's id in the labels of the machines it rents (6 hex digits, made once): on a Vast account
    shared with teammates, their machines must never look like ours."""
    path = home() / "install_id"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(os.urandom(3).hex() + "\n", encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def vast_key_file() -> Path:
    """The Vast key the vastai CLI keeps: switching it switches every Vast tool at once."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "vastai" / "vast_api_key"


def vast_session_file() -> Path:
    """The 2FA session key `vastai tfa login` saves: while it exists, the CLI sends it instead of the key."""
    return vast_key_file().with_name("vast_tfa_key")


def tool(name: str) -> str | None:
    """A program on PATH. On Windows, ssh and ssh-keygen are Windows' own OpenSSH, the one Windows
    Terminal and VS Code use (Git Bash puts a different ssh first on PATH)."""
    if os.name == "nt" and name in ("ssh", "ssh-keygen"):
        own = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "OpenSSH" / f"{name}.exe"
        if own.exists():
            return str(own)
    return shutil.which(name)


# ---- VS Code's user settings ---------------------------------------------------------------------


def vscode_settings_file() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        base = home / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return base / "Code" / "User" / "settings.json"


def read_vscode_settings(path: Path) -> dict | None:
    """The settings, or None when the file is not plain JSON (VS Code allows comments there)."""
    if not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return settings if isinstance(settings, dict) else None


def write_vscode_settings(path: Path, settings: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(settings, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    replace(tmp, path)


# ---- settings ------------------------------------------------------------------------------------

DEFAULTS: dict[str, object] = {
    "approve_above_usd_per_hour": 5.0,
    "idle_minutes": 60,
    "image": "vastai/base-image:cuda-12.8.1-auto",
    "disk_gb": 64,
    "min_reliability": 0.98,
    "min_download_mbps": 200.0,
    "regions": [],
    "home": "",
    "owner": "",
    "mode": "colleague",
    "model": "",
    "effort": "",
    "fast_mode": "",
    "workspace_repo": "agent-workspace",
    "notify_url": "",
    "open_window": "on",
}
MODES = ("autonomous", "colleague", "interactive")
EFFORTS = ("low", "medium", "high", "xhigh")


def _regions_ok(value: list) -> bool:
    from crab_student.offers import REGIONS

    return all(r.lower() in REGIONS or re.fullmatch(r"[A-Za-z]{2}", r) for r in value)


RULES = {
    "approve_above_usd_per_hour": (lambda v: v >= 0, "a number of dollars per hour, 0 or more"),
    "idle_minutes": (lambda v: v >= 1, "a whole number of minutes, 1 or more"),
    "image": (bool, "a Docker image, e.g. vastai/base-image:cuda-12.8.1-auto"),
    "disk_gb": (lambda v: v >= 10, "a whole number of GB, 10 or more"),
    "min_reliability": (lambda v: 0 <= v <= 1, "a number from 0 to 1"),
    "min_download_mbps": (lambda v: v >= 0, "a number of Mbps, 0 or more"),
    "regions": (
        _regions_ok,
        "country codes (US, CA, DE, ...) or continents (north-america, south-america, "
        "europe, asia, oceania, africa), comma-separated; empty means anywhere",
    ),
    "home": (
        lambda v: v == "" or bool(re.fullmatch(r"[A-Za-z]{2}", v)),
        "your country code (CA, US, DE, ...), so that the launcher weighs how far a host is from you; empty: "
        "not set",
    ),
    "owner": (
        lambda v: bool(re.fullmatch(r"[A-Za-z0-9]*", v)),
        "letters and digits, e.g. SFALLOWS: how your machines' labels on Vast start (empty: crab-student)",
    ),
    "mode": (lambda v: v in MODES, "autonomous, colleague or interactive"),
    "model": (lambda v: True, "a Claude model such as opus or sonnet (empty: as your own Claude Code)"),
    "effort": (lambda v: v in ("", *EFFORTS), "low, medium, high or xhigh (empty: as your own Claude Code)"),
    "fast_mode": (lambda v: v in ("", "on", "off"), "on or off (empty: as your own Claude Code)"),
    "workspace_repo": (lambda v: bool(re.fullmatch(r"[A-Za-z0-9._-]+", v)), "a GitHub repo name"),
    "notify_url": (lambda v: v == "" or v.startswith(("http://", "https://")), "an http(s) URL, or empty"),
    "open_window": (lambda v: v in ("on", "off"), "on or off"),
}


def parse_value(key: str, text: str) -> object:
    """A setting's value from text, by its default's type ("1.5", "CA, europe"); CrabError(2) if invalid."""
    if key not in DEFAULTS:
        raise CrabError(f"there is no setting {key!r}. Settings: {', '.join(DEFAULTS)}", 2)
    default, value = DEFAULTS[key], None
    with contextlib.suppress(ValueError):
        if isinstance(default, list):
            value = [part.strip() for part in text.split(",") if part.strip()]
        elif isinstance(default, int):
            value = int(text)
        elif isinstance(default, float):
            value = float(text) if math.isfinite(float(text)) else None
        else:
            value = text.strip()
    check, rule = RULES[key]
    if value is None or not check(value):
        raise CrabError(f"{key} must be {rule}, not {text!r}.", 2)
    return value


def load_config() -> dict:
    data = read_json(home() / "config.json", {})
    return {
        k: (list(v) if isinstance(v, list) else v) for k, v in {**DEFAULTS, **data}.items() if k in DEFAULTS
    }


def set_config(key: str, text: str | None) -> object:
    """Set a setting from text, or reset it to its default (None). Returns the value now in force."""
    value = DEFAULTS[key] if text is None and key in DEFAULTS else parse_value(key, text or "")
    data = {k: v for k, v in read_json(home() / "config.json", {}).items() if k in DEFAULTS and k != key}
    if value != DEFAULTS[key]:
        data[key] = value
    write_json(home() / "config.json", data)
    return value


def own_claude_settings() -> dict:
    """What agents copy from the user's own Claude Code settings when nothing else says: `model`,
    `effortLevel`, `fastMode`, and the effort levels in `modelSettings` (where Claude Code keeps the effort
    chosen for each model), those set with a sensible type (a missing or invalid file gives none)."""
    data = read_json(Path.home() / ".claude" / "settings.json", {})
    if not isinstance(data, dict):
        return {}
    kept: dict = {}
    if isinstance(data.get("model"), str) and data["model"].strip():
        kept["model"] = data["model"].strip()
    if data.get("effortLevel") in EFFORTS:
        kept["effortLevel"] = data["effortLevel"]
    if isinstance(data.get("fastMode"), bool):
        kept["fastMode"] = data["fastMode"]
    per_model = data.get("modelSettings") if isinstance(data.get("modelSettings"), dict) else {}
    if efforts := {
        m: {"effortLevel": s["effortLevel"]}
        for m, s in per_model.items()
        if isinstance(s, dict) and s.get("effortLevel") in EFFORTS
    }:
        kept["modelSettings"] = efforts
    return kept


# ---- agent records -------------------------------------------------------------------------------

NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]")


def check_name(name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise CrabError(
            f'"{name}" is not a valid name: use 3-40 characters from a-z, 0-9 and "-" (not at the start or end).',
            2,
        )


def record_path(name: str) -> Path:
    return agents_dir() / f"{name}.json"


def load(name: str) -> dict:
    rec = read_json(record_path(name), None)
    if rec is None:
        raise CrabError(f"there is no agent named {name} (see crab-student list).")
    return rec


def all_records() -> list[dict]:
    recs = [read_json(p, None) for p in sorted(agents_dir().glob("*.json"))]
    return sorted((r for r in recs if r is not None), key=lambda r: r.get("created", ""))


def save(rec: dict) -> None:
    write_json(record_path(rec["name"]), rec)


def remove(name: str) -> None:
    """Forget an agent: its record, brief and launch log (its lock file may still be held)."""
    for suffix in (".json", ".brief.md", ".log", ".lock"):
        with contextlib.suppress(OSError):
            (agents_dir() / f"{name}{suffix}").unlink(missing_ok=True)


def new_session(rec: dict) -> None:
    """The machine runs again after a stop, and counts its hours from 0 again: the record's totals take the
    machine hours last seen and the time it sat stopped (its disk was billed), for what the agent has cost."""
    last = rec.get("last_seen") or {}
    rec["past_hours"] = (rec.get("past_hours") or 0) + (last.get("used_hours") or 0)
    if last:
        rec["last_seen"] = {**last, "used_hours": 0}
    if rec.get("stopped_since"):
        stopped = (time.time() - rec["stopped_since"]) / 3600
        rec["stopped_hours"] = (rec.get("stopped_hours") or 0) + max(0.0, stopped)


def spend_file() -> Path:
    """What deleted agents cost: a JSON object per line (name, machine, created, deleted, hours, usd)."""
    return home() / "spend.jsonl"


def book_spend(entry: dict) -> None:
    spend_file().parent.mkdir(parents=True, exist_ok=True)
    with open(spend_file(), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def read_spend() -> list[dict]:
    if not spend_file().exists():
        return []
    return [json.loads(line) for line in spend_file().read_text(encoding="utf-8").splitlines()]


@contextlib.contextmanager
def locked(name: str, wait: float = 0) -> Iterator[None]:
    """Hold the agent's OS lock (it dies with the process, so a lock that cannot be taken means another
    crab-student command is working on the agent right now)."""
    path = agents_dir() / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        deadline = time.monotonic() + wait
        while not _lock(f):
            if time.monotonic() >= deadline:
                raise CrabError(
                    f"another crab-student command is working on {name} right now. Try again when "
                    "it has finished."
                )
            time.sleep(0.5)
        try:
            yield
        finally:
            _unlock(f)


def is_locked(name: str) -> bool:
    try:
        with locked(name):
            return False
    except CrabError:
        return True


def _lock(f) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(f) -> None:
    with contextlib.suppress(OSError):
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


# ---- files ---------------------------------------------------------------------------------------


def read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data: object) -> None:
    """Write atomically (a reader never sees half a file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    replace(tmp, path)


def replace(tmp: Path | str, path: Path) -> None:
    for attempt in range(5):  # (Windows refuses to replace a file another process has open for a moment)
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1)


def write_private(path: Path, text: str) -> None:
    """A file only the user can read (0600 where that means something)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


# ---- secrets -------------------------------------------------------------------------------------

SERVICE = "crab-student"
TOKEN = "claude-oauth-token"  # the Claude token for agents
SECRET_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


def secret_key(name: str) -> str:
    return f"task-secret:{name}"


def _fallback(key: str) -> Path:
    return home() / "secrets" / key.replace(":", "_")


def get_secret(key: str) -> str | None:
    with contextlib.suppress(keyring.errors.KeyringError, RuntimeError):
        if value := keyring.get_password(SERVICE, key):
            return value
    try:
        return _fallback(key).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def set_secret(key: str, value: str) -> str:
    """Store a secret; returns where it went ("keyring" or "file", when no keyring works)."""
    try:
        keyring.set_password(SERVICE, key, value)
        kept = keyring.get_password(SERVICE, key) == value  # (the null backend drops it without a word)
    except (keyring.errors.KeyringError, RuntimeError):
        kept = False
    if not kept:
        write_private(_fallback(key), value)
        return "file"
    _fallback(key).unlink(missing_ok=True)
    return "keyring"


def delete_secret(key: str) -> None:
    with contextlib.suppress(keyring.errors.KeyringError, RuntimeError):
        keyring.delete_password(SERVICE, key)
    _fallback(key).unlink(missing_ok=True)


def secret_names() -> list[str]:
    return sorted(read_json(home() / "secrets.json", {}).get("names", []))


def set_secret_names(names: list[str]) -> None:
    write_json(home() / "secrets.json", {"names": sorted(set(names))})


# ---- subprocesses --------------------------------------------------------------------------------


def run(
    cmd: list[str],
    *,
    input: str | bytes | None = None,
    cwd: Path | str | None = None,
    env: dict | None = None,
    timeout: float = 120,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a program (text mode unless `input` is bytes); CrabError with its last error line on failure."""
    text = not isinstance(input, bytes)
    name = Path(cmd[0]).stem
    try:
        proc = subprocess.run(
            cmd,
            input=input,
            cwd=cwd,
            env=env,
            timeout=timeout,
            capture_output=True,
            text=text,
            encoding="utf-8" if text else None,
            errors="replace" if text else None,
        )
    except FileNotFoundError:
        raise CrabError(f"{name} was not found: install it, or add it to your PATH.") from None
    except subprocess.TimeoutExpired:
        raise CrabError(f"{name} did not finish within {timeout:.0f} s.") from None
    if check and proc.returncode != 0:
        out = proc.stderr or proc.stdout
        raise CrabError(f"{name} failed: {last_line(out if text else out.decode('utf-8', 'replace'))}")
    return proc


def keygen(path: Path, comment: str) -> None:
    """A new ed25519 key without a passphrase, made by ssh-keygen (so Windows gives it the ACLs OpenSSH wants)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    run([tool("ssh-keygen") or "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)])


def last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


# ---- formatting ----------------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def usd(x: float | None) -> str:
    return "-" if x is None else f"${x:.2f}"


def fmt_hours(h: float | None) -> str:
    if h is None:
        return "-"
    minutes = max(0, round(h * 60))
    return f"{minutes // 60}h{minutes % 60:02d}m"


def ascii_text(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


# ---- the terminal --------------------------------------------------------------------------------


def interactive() -> bool:
    """Someone is at this terminal (stdin and stdout are both one)."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def say(text: str = "", err: bool = False) -> None:
    """A line on the terminal (stderr with `err`), in ASCII."""
    print(ascii_text(text), file=sys.stderr if err else sys.stdout, flush=True)
