"""`crab-student init`, the Claude Code integration (skill and permission rules) and readiness checks."""

from __future__ import annotations

import getpass
import re
import subprocess
import sys
from pathlib import Path

from crab_student import core, github, vast

DATA = Path(__file__).parent / "data"
TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{60,}")

# Allowed without asking: reading, and actions that stay in budget by construction (launch, start and
# extend check the approval gate themselves). Always asked: spending beyond the gate, destroying,
# discarding, and changing settings.
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
        "extend",
        "secret list",
    )
]
ASK = [
    f"Bash(crab-student {c}:*)"
    for c in ("approve", "decline", "delete", "config", "init", "secret set", "secret delete")
]
_OURS = re.compile(r"^Bash\(crab-student[ :)]")


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask_secret(prompt: str) -> str:
    if not interactive():
        raise core.CrabError("init needs to ask you something: run crab-student init in a terminal.", 2)
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
        raise core.CrabError("crab-student has no Claude token for workers yet. Run: crab-student init")


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
        key.parent.mkdir(parents=True, exist_ok=True)
        core.run(
            [
                core.tool("ssh-keygen") or "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "crab-student",
                "-f",
                str(key),
            ]
        )
    github.known_hosts_file().write_text(github.HOST_KEY + "\n", encoding="utf-8")
    added = ensure_ssh_include()
    note = " (and added the Include line to ~/.ssh/config)" if added else ""
    _say("SSH key", f"{'created' if made else 'ok -'} {key}{note}")
    return True


def ensure_ssh_include() -> bool:
    """Make ~/.ssh/config include crab-student's host entries (first line: a later one could fall inside a
    Host block). True if it was added."""
    config = Path.home() / ".ssh" / "config"
    line = f'Include "{str(core.ssh_dir() / "config").replace(chr(92), "/")}"'
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    if line in text:
        return False
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"{line}\n{text}", encoding="utf-8")
    return True


def _claude_step(renew: bool) -> bool:
    if core.get_secret(core.TOKEN) and not renew:
        _say("Claude", "ok - a token for workers is stored")
        return True
    print("  Claude      Workers use your Claude subscription through a long-lived token.")
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
    split = (
        f"{extra} found (open --all works)"
        if core.tool(extra)
        else f"no {extra} (only needed for open --all)"
    )
    _say("Tools", f"missing: {', '.join(missing)}" if missing else f"ssh, git, gh ok; {split}")
    return not missing


def install_claude_files() -> None:
    """The skill, and the permission rules merged into ~/.claude/settings.json (older crab-student rules are
    replaced; everything else is kept)."""
    claude = Path.home() / ".claude"
    skill = claude / "skills" / "crab-student" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text((DATA / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8")
    path = claude / "settings.json"
    settings = core.read_json(path, None) if path.exists() else {}
    if not isinstance(settings, dict):
        _say(
            "",
            f"Warning: {path} is not valid JSON, so the permission rules were not added. Fix it and run init again.",
        )
        return
    perms = settings.setdefault("permissions", {})
    for kind, ours in (("allow", ALLOW), ("ask", ASK)):
        kept = [r for r in perms.get(kind) or [] if not _OURS.match(r)]
        perms[kind] = kept + ours
    core.write_json(path, settings)


def guide(topic: str) -> str:
    if topic != "launch":
        raise core.CrabError("the only guide is: crab-student guide launch", 2)
    return (DATA / "launch.md").read_text(encoding="utf-8")
