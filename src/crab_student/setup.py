"""`crab-student init`, the Claude Code integration (skill, permission rules) and readiness checks."""

from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from crab_student import core, github, vast

DATA = Path(__file__).parent / "data"
OLD_LAUNCHER_AGENT = "agents/crab-student-launcher.md"  # (in ~/.claude; init used to install it)
FORK_VERSION = (2, 1, 232)  # (the first Claude Code whose forks the skill starts the launcher with)
VSCODE_EXTENSIONS = "remote.SSH.defaultExtensions"
CLAUDE_EXTENSION = "anthropic.claude-code"
TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{60,}")

# Allowed without asking: reading, and actions within the user's price rule (a launch that would need a
# pricier machine waits for `approve`). Always asked: approving, destroying, discarding, and changing
# settings.
ALLOW = [
    f"Bash(crab-student {c}:*)"
    for c in (
        "guide",
        "offers",
        "launch",
        "list",
        "status",
        "report",
        "pull",
        "send",
        "open",
        "stop",
        "start",
        "secret list",
    )
]
ASK = [
    f"Bash(crab-student {c}:*)"
    for c in ("approve", "decline", "delete", "config", "init", "secret set", "secret delete")
]
# Claude Code may not raise the limits by editing crab-student's files instead of asking.
DENY = ["Edit(~/.crab-student/**)", "Write(~/.crab-student/**)"]
_OURS = re.compile(r"^(Bash\(crab-student[ :)]|(Edit|Write)\(~/\.crab-student/)")
NOT_A_TERMINAL = (
    "this is not an interactive terminal. Run it in PowerShell, Windows Terminal, or macOS/Linux's terminal "
    "(in Git Bash's own window, put winpty in front)."
)


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask_secret(prompt: str) -> str:
    if not interactive():
        raise core.CrabError(f"init needs to ask you something, but {NOT_A_TERMINAL}", 2)
    return getpass.getpass(prompt).strip()


def _say(label: str, text: str) -> None:
    print(core.ascii_text(f"  {label:<11} {text}"), flush=True)


def check_ready(fake_claude: bool = False) -> None:
    """What launch and offers need; each missing piece says to run init."""
    if not core.vast_key_file().exists():
        raise core.CrabError("crab-student is not set up yet (no Vast key). Run: crab-student init")
    if not (core.ssh_dir() / "id_ed25519").exists() or not github.known_hosts_file().exists():
        raise core.CrabError("crab-student is not set up yet (no SSH key). Run: crab-student init")
    if not fake_claude and not core.get_secret(core.TOKEN):
        raise core.CrabError("crab-student has no Claude token for agents yet. Run: crab-student init")


def init(renew_claude_token: bool = False) -> int:
    print("crab-student setup")
    ok = all([_vast_step(), _github_step(), _ssh_step(), _claude_step(renew_claude_token)])
    install_claude_files()
    _say("Claude Code", "skill and permission rules installed in ~/.claude")
    _claude_version_step()
    _vscode_step()
    _home_step()
    ok = _tools_step() and ok
    if ok:
        print(
            'Ready. In Claude Code, ask for a task to run on a GPU, e.g. "run the ablation on a GPU with crab-student".'
        )
    return 0 if ok else 1


def _vast_step() -> bool:
    path = core.vast_key_file()
    key = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    for attempt in range(3):
        if not key:
            print(
                "  Vast        Create an API key at https://cloud.vast.ai/manage-keys/ (it is saved where the vastai "
                "CLI keeps it)."
            )
            key = _ask_secret("              Paste it here (it will not be shown): ")
        try:
            user = vast.current_user(key=key)
        except core.CrabError as e:
            _say("Vast", f"that key does not work: {e.message}")
            key = ""
            if attempt == 2 or not interactive():
                return False
            continue
        if not path.exists() or path.read_text(encoding="utf-8").strip() != key:
            core.write_private(path, key + "\n")
        credit = user.get("credit")
        who = user.get("email") or user.get("username") or "your account"
        _say("Vast", f"ok - {who}" + (f", credit ${credit:.2f}" if isinstance(credit, int | float) else ""))
        if isinstance(credit, int | float) and credit <= 0:
            _say("", "Add credit at https://cloud.vast.ai/billing/ before launching.")
        return True
    return False


def _github_step() -> bool:
    gh = core.tool("gh")
    if not gh:
        _say(
            "GitHub",
            "gh (GitHub's CLI) is missing: install it from https://cli.github.com, then run init again.",
        )
        return False
    if subprocess.run([gh, "auth", "status"], capture_output=True).returncode != 0:
        if not interactive():
            _say("GitHub", "not logged in: run gh auth login in a terminal, then init again.")
            return False
        _say("GitHub", "logging in with gh auth login:")
        if subprocess.call([gh, "auth", "login"]) != 0:
            return False
    _say("GitHub", f"ok - logged in as {github.login()}")
    return True


def _ssh_step() -> bool:
    key = core.ssh_dir() / "id_ed25519"
    made = not key.exists()
    if made:
        core.keygen(key, "crab-student")
    github.known_hosts_file().write_text(github.HOST_KEY + "\n", encoding="utf-8")
    added = ensure_ssh_include()
    note = " (and added the Include line to ~/.ssh/config)" if added else ""
    _say("SSH key", f"{'created' if made else 'ok -'} {key}{note}")
    return True


def ensure_ssh_include() -> bool:
    """Make ~/.ssh/config include crab-student's host entries (first line: a later one could fall inside a
    Host block). True if it was added. An Include of the same file spelled another way is replaced."""
    config = Path.home() / ".ssh" / "config"
    target = core.ssh_dir() / "config"
    line = f"Include {core.ssh_path(target)}"
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    if text.startswith(line + "\n"):
        return False
    tail = "/".join(target.parts[-3:])  # (".crab-student/ssh/config", however the rest is spelled)
    kept = [
        s for s in text.splitlines() if not (s.startswith("Include") and s.strip().strip('"').endswith(tail))
    ]
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join([line, *kept]) + "\n", encoding="utf-8")
    return True


def _claude_step(renew: bool) -> bool:
    if core.get_secret(core.TOKEN) and not renew:
        _say("Claude", "ok - a token for agents is stored")
        return True
    print("  Claude      Agents use your Claude subscription through a long-lived token.")
    print("              In another terminal, run: claude setup-token")
    for _ in range(3):
        token = "".join(
            _ask_secret("              Paste the token (sk-ant-oat01-...), it will not be shown: ").split()
        )
        if TOKEN_RE.fullmatch(token):
            where = core.set_secret(core.TOKEN, token)
            _say(
                "",
                "ok - stored in your system keyring"
                if where == "keyring"
                else f"ok - stored in {core.home() / 'secrets'} (no system keyring works here)",
            )
            return True
        _say("", "that is not a whole token (sk-ant-oat01- and about 90 more characters). Copy all of it.")
    return False


def _tools_step() -> bool:
    need = {n: core.tool(n) for n in ("ssh", "ssh-keygen", "git", "gh")}
    missing = [n for n, path in need.items() if not path]
    extra = "wt" if sys.platform == "win32" else "tmux"
    uses = "the window that follows a launch, and open --all" if extra == "wt" else "open --all"
    split = f"{extra} found (for {uses})" if core.tool(extra) else f"no {extra} (only needed for {uses})"
    _say("Tools", f"missing: {', '.join(missing)}" if missing else f"ssh, git, gh ok; {split}")
    return not missing


def claude_version() -> tuple[int, ...] | None:
    claude = core.tool("claude")
    if not claude:
        return None
    try:
        out = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(n) for n in found.groups()) if found else None


def _claude_version_step() -> None:
    version = claude_version()
    if version is None:
        _say(
            "Claude Code", "could not check its version; the launcher needs v2.1.232 or later (claude update)"
        )
    elif version < FORK_VERSION:
        need = ".".join(map(str, FORK_VERSION))
        _say(
            "Claude Code",
            f"Warning: v{'.'.join(map(str, version))} is older than v{need}, which the launcher needs "
            "(it starts as a fork of your conversation). Run: claude update",
        )
    else:
        _say("Claude Code", f"ok - v{'.'.join(map(str, version))}")


def _home_step() -> None:
    if home := core.load_config()["home"]:
        _say("Home", f"{home.upper()} - interactive agents get hosts near it")
        return
    guess = guess_country()
    _say(
        "Home",
        "not set. So interactive agents get hosts near you (less lag when you type), run: "
        f"crab-student config home {guess or 'XX'}" + ("" if guess else " (XX: your country code)"),
    )


def guess_country() -> str:
    """The country of the user's locale ("fr_CA.UTF-8", or Windows' "en-CA" -> "CA"), else ""."""
    names = [os.environ.get(k, "") for k in ("LC_ALL", "LC_CTYPE", "LANG")]
    if os.name == "nt":
        import ctypes

        buf = ctypes.create_unicode_buffer(85)
        names.append(buf.value if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85) else "")
    for name in names:
        if m := re.match(r"[a-z]{2,3}[_-]([A-Z]{2})(?![A-Za-z])", name):
            return m.group(1)
    return ""


def _vscode_step() -> None:
    """Offer to have VS Code install the Claude extension on every machine it connects to (Remote - SSH)."""
    path = core.vscode_settings_file()
    if not path.parent.exists():
        _say("VS Code", "not found (only needed for open --vscode, which uses its Remote - SSH extension)")
        return
    hint = f'add "{VSCODE_EXTENSIONS}": ["{CLAUDE_EXTENSION}"] to {path}'
    settings = core.read_vscode_settings(path)
    current = settings.get(VSCODE_EXTENSIONS) if settings is not None else None
    if settings is None or (current is not None and not isinstance(current, list)):
        _say("VS Code", f"could not edit {path} safely. So the Claude extension installs on machines: {hint}")
    elif CLAUDE_EXTENSION in (current or []):
        _say("VS Code", "ok - the Claude extension installs on machines by itself")
    elif interactive() and _confirm(
        f"  VS Code     Install the Claude extension on machines by itself ({VSCODE_EXTENSIONS})? [Y/n] "
    ):
        settings[VSCODE_EXTENSIONS] = [*(current or []), CLAUDE_EXTENSION]
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(settings, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        core.replace(tmp, path)
        _say("VS Code", f"ok - added {VSCODE_EXTENSIONS} to {path}")
    else:
        _say("VS Code", f"to have the Claude extension installed on machines by itself, {hint}")


def _confirm(prompt: str) -> bool:
    return input(prompt).strip().lower() in ("", "y", "yes")


def install_claude_files() -> None:
    """The skill, and the permission rules merged into ~/.claude/settings.json (older crab-student rules
    are replaced; everything else is kept). The launcher agent of older versions is removed."""
    claude = Path.home() / ".claude"
    (claude / OLD_LAUNCHER_AGENT).unlink(missing_ok=True)
    path = claude / "skills/crab-student/SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((DATA / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8")
    path = claude / "settings.json"
    settings = core.read_json(path, None) if path.exists() else {}
    if not isinstance(settings, dict):
        _say(
            "",
            f"Warning: {path} is not valid JSON, so the permission rules were not added. Fix it and run init again.",
        )
        return
    perms = settings.setdefault("permissions", {})
    for kind, ours in (("allow", ALLOW), ("ask", ASK), ("deny", DENY)):
        kept = [r for r in perms.get(kind) or [] if not _OURS.match(r)]
        perms[kind] = kept + ours
    core.write_json(path, settings)


def guide(topic: str) -> str:
    if topic != "launch":
        raise core.CrabError("the only guide is: crab-student guide launch", 2)
    price = core.load_config()["approve_above_usd_per_hour"]
    text = (DATA / "launch.md").read_text(encoding="utf-8")
    return text.replace("{{approve_above_usd_per_hour}}", f"{price:g}")
