"""`crab-student init`, the Claude Code integration (skill, permission rules) and readiness checks."""

from __future__ import annotations

import getpass
import re
import subprocess
import sys
from pathlib import Path

from crab_student import core, github, vast

DATA = Path(__file__).parent / "data"
TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{60,}")

# Allowed without asking: reading (fetch copies an agent's output files into a new folder NAME-outputs here),
# actions within the user's price rule (a launch that would need a pricier machine waits for `approve`), and
# deleting (`delete` refuses to lose work unless forced). Always asked: approving, forcing a delete, changing
# settings and secrets, and a fetch into a folder of the caller's choice (the machine chooses the file names
# and what is in them, and some folders give files a meaning: ~/.claude, .github, a folder on the PATH).
ALLOW = [
    f"Bash(crab-student {c}:*)"
    for c in (
        "guide",
        "offers",
        "launch",
        "list",
        "status",
        "wait",
        "spend",
        "report",
        "pull",
        "fetch",
        "send",
        "open",
        "stop",
        "start",
        "delete",
        "decline",
        "secret list",
    )
]
ASK = [
    *(f"Bash(crab-student {c}:*)" for c in ("approve", "config", "init", "secret set", "secret delete")),
    "Bash(crab-student delete *--force*)",  # (an ask rule wins over the allow rule for delete, and for fetch)
    "Bash(crab-student fetch *--to*)",
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
    ok = _tools_step() and ok
    if ok:
        print(
            'Ready. In Claude Code, ask for a task to run on a GPU, e.g. "run the ablation on a GPU with crab-student".'
        )
    return 0 if ok else 1


def _vast_step() -> bool:
    path = core.vast_key_file()
    saved = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    if not saved:
        print(
            "  Vast        Create an API key at https://cloud.vast.ai/manage-keys/ (it is saved where the vastai "
            "CLI keeps it)."
        )
    key = saved or _ask_secret("              Paste it here (it will not be shown): ")
    try:
        user = vast.current_user(key=None if saved else key)  # (a saved key is sent with its 2FA session)
    except core.CrabError as e:
        _say("Vast", f"that key does not work: {e.message}")
        _say("", f"To paste another, delete {path}, then run init again." if saved else "Run init again.")
        return False
    if key != saved:
        core.write_private(path, key + "\n")
    credit = user.get("credit")
    who = user.get("email") or user.get("username") or "your account"
    _say("Vast", f"ok - {who}" + (f", credit ${credit:.2f}" if isinstance(credit, int | float) else ""))
    if isinstance(credit, int | float) and credit <= 0:
        _say("", "Add credit at https://cloud.vast.ai/billing/ before launching.")
    return True


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
    Host block). True if it was added."""
    config = Path.home() / ".ssh" / "config"
    line = f"Include {core.ssh_path(core.ssh_dir() / 'config')}"
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    if text.startswith(line + "\n"):
        return False
    kept = [s for s in text.splitlines() if s != line]
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


def install_claude_files() -> None:
    """The skill, and the permission rules merged into ~/.claude/settings.json (older crab-student rules
    are replaced; everything else is kept)."""
    claude = Path.home() / ".claude"
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
    home = core.load_config()["home"].upper()
    text = (DATA / "launch.md").read_text(encoding="utf-8")
    return text.replace("{{home}}", home or "an unknown country")
