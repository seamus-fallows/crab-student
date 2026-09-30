"""Errors, paths, settings, worker records and their locks, secrets, subprocesses, formatting."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import subprocess
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


def workers_dir() -> Path:
    return home() / "workers"


def ssh_dir() -> Path:
    return home() / "ssh"


def vast_key_file() -> Path:
    """The Vast key the vastai CLI keeps: switching it switches every Vast tool at once."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "vastai" / "vast_api_key"


def tool(name: str) -> str | None:
    """A program on PATH. On Windows, ssh and ssh-keygen are Windows' own OpenSSH, the one Windows
    Terminal and VS Code use (Git Bash puts a different ssh first on PATH)."""
    if os.name == "nt" and name in ("ssh", "ssh-keygen"):
        own = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "OpenSSH" / f"{name}.exe"
        if own.exists():
            return str(own)
    return shutil.which(name)


# ---- settings ------------------------------------------------------------------------------------

DEFAULTS: dict[str, object] = {
    "auto_approve_usd": 50.0,
    "max_running_machines": 5,
    "budget_factor": 2.0,
    "min_budget_hours": 2.0,
    "idle_minutes": 60,
    "image": "vastai/base-image:cuda-12.8.1-auto",
    "disk_gb": 50,
    "min_reliability": 0.98,
    "min_download_mbps": 200.0,
    "regions": [],
    "model": "",
    "workspace_repo": "crab-student-work",
    "notify_url": "",
}


def _regions_ok(value: list) -> bool:
    from crab_student.offers import REGIONS

    return all(r.lower() in REGIONS or re.fullmatch(r"[A-Za-z]{2}", r) for r in value)


RULES = {
    "auto_approve_usd": (lambda v: v >= 0, "a number of dollars, 0 or more"),
    "max_running_machines": (lambda v: v >= 1, "a whole number, 1 or more"),
    "budget_factor": (lambda v: v >= 1, "a number, 1 or more"),
    "min_budget_hours": (lambda v: v > 0, "a number of hours above 0"),
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
    "model": (lambda v: True, "a Claude model such as opus or sonnet; empty uses Claude Code's default"),
    "workspace_repo": (lambda v: bool(re.fullmatch(r"[A-Za-z0-9._-]+", v)), "a GitHub repo name"),
    "notify_url": (lambda v: v == "" or v.startswith(("http://", "https://")), "an http(s) URL, or empty"),
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


# ---- worker records ------------------------------------------------------------------------------

NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,38}[a-z0-9]")


def check_name(name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise CrabError(
            f'"{name}" is not a valid name: use 3-40 characters from a-z, 0-9 and "-" (not at the start or end).',
            2,
        )


def record_path(name: str) -> Path:
    return workers_dir() / f"{name}.json"


def load(name: str) -> dict:
    rec = read_json(record_path(name), None)
    if rec is None:
        raise CrabError(f"there is no worker named {name} (see crab-student list).")
    return rec


def all_records() -> list[dict]:
    recs = [read_json(p, None) for p in sorted(workers_dir().glob("*.json"))]
    return sorted(
        (r for r in recs if isinstance(r, dict) and "name" in r), key=lambda r: r.get("created", "")
    )


def save(rec: dict) -> None:
    write_json(record_path(rec["name"]), rec)


def remove(name: str) -> None:
    """Forget a worker: its record, brief and deploy key files (its lock file may still be held)."""
    for suffix in (".json", ".brief.md", ".key", ".key.pub", ".lock"):
        with contextlib.suppress(OSError):
            (workers_dir() / f"{name}{suffix}").unlink(missing_ok=True)


@contextlib.contextmanager
def locked(name: str, wait: float = 0) -> Iterator[None]:
    """Hold the worker's OS lock (it dies with the process, so a lock that cannot be taken means another
    crab-student command is working on the worker right now)."""
    path = workers_dir() / f"{name}.lock"
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
TOKEN = "claude-oauth-token"  # the Claude token for workers
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


def last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


# ---- formatting ----------------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ceil_cents(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100


def usd(x: float | None) -> str:
    return "-" if x is None else f"${x:.2f}"


def fmt_hours(h: float | None) -> str:
    if h is None:
        return "-"
    minutes = max(0, round(h * 60))
    return f"{minutes // 60}h{minutes % 60:02d}m"


def ascii_text(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")
