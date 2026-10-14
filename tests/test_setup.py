"""init, readiness, the Claude Code integration (skill, permission rules) and the package's Markdown."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from crab_student import cli, core, github, setup

TOKEN = "sk-ant-oat01-" + "t" * 90


@pytest.mark.parametrize("missing", ["vast key", "ssh key", "token"])
def test_check_ready_needs_every_piece(missing, ready):
    core.write_private(core.vast_key_file(), "key\n")
    setup.check_ready()
    remove = {"vast key": core.vast_key_file().unlink, "ssh key": (core.ssh_dir() / "id_ed25519").unlink,
              "token": lambda: core.delete_secret(core.TOKEN)}  # fmt: skip
    remove[missing]()
    with pytest.raises(core.CrabError) as e:
        setup.check_ready()
    assert "crab-student init" in e.value.message
    if missing == "token":
        setup.check_ready(fake_claude=True)  # (a fake-Claude launch needs no token)


@pytest.fixture
def terminal(fake_vast, fake_github, monkeypatch) -> tuple[list[str], list[list[str]]]:
    """init in a terminal, gh logged in, ssh-keygen faked: the test lists what the user types; returns
    (answers, ssh-keygen calls). Asking for more than the answers fails the test."""
    answers: list[str] = []
    keygens: list[list[str]] = []

    def ask(prompt: str = "") -> str:
        assert answers, f"init asked: {prompt}"
        return answers.pop(0)

    def keygen(cmd: list[str], **kwargs) -> None:
        keygens.append(cmd)
        key = Path(cmd[cmd.index("-f") + 1])
        key.write_text("private\n")
        Path(f"{key}.pub").write_text("ssh-ed25519 AAAA crab-student\n")

    def no_login(*args, **kwargs):
        raise AssertionError("gh auth login ran although gh is logged in")

    logged_in = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0), call=no_login)
    monkeypatch.setattr(setup, "subprocess", logged_in)  # (gh auth status)
    monkeypatch.setattr(setup, "interactive", lambda: True)
    monkeypatch.setattr(setup.getpass, "getpass", ask)
    monkeypatch.setattr(core, "tool", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(core, "run", keygen)
    return answers, keygens


def test_init_asks_only_for_what_is_missing(terminal, fake_vast, isolated, capsys):
    answers, keygens = terminal
    core.vast_key_file().unlink()
    fake_vast.user["email"] = "adé@example.com"
    answers += ["wrong-key", "sk-ant-oat01-too-short", f"  {TOKEN}  "]
    assert setup.init() == 1  # (a Vast key that does not work is asked for once, and not saved)
    assert not answers and not core.vast_key_file().exists()
    answers.append(fake_vast.key)
    assert setup.init() == 0
    assert not answers and len(keygens) == 1
    assert core.vast_key_file().read_text().strip() == fake_vast.key  # (where the vastai CLI keeps it)
    assert core.get_secret(core.TOKEN) == TOKEN
    assert github.known_hosts_file().read_text().strip() == github.HOST_KEY
    assert (isolated / ".ssh" / "config").read_text().startswith("Include ")
    assert json.loads((isolated / ".claude" / "settings.json").read_text())["permissions"]["ask"] == setup.ASK
    assert setup.init() == 0  # everything is there now: nothing is asked, no new key
    assert len(keygens) == 1
    answers.append("sk-ant-oat01-" + "n" * 90)
    assert setup.init(renew_claude_token=True) == 0
    assert core.get_secret(core.TOKEN) == "sk-ant-oat01-" + "n" * 90
    assert capsys.readouterr().out.isascii()


def test_a_saved_vast_key_that_does_not_work_fails_init_without_asking(terminal, ready, fake_vast):
    core.write_private(core.vast_key_file(), "revoked-key\n")
    assert setup.init() == 1  # (asking would fail the test: the terminal fixture has no answers)
    assert core.vast_key_file().read_text() == "revoked-key\n"


def test_init_checks_a_saved_vast_key_with_its_2fa_session(terminal, ready, fake_vast):
    core.write_private(core.vast_key_file(), "key-that-needs-2fa\n")
    core.write_private(core.vast_session_file(), fake_vast.key + "\n")
    assert setup.init() == 0
    assert core.vast_key_file().read_text() == "key-that-needs-2fa\n"


def test_init_fails_after_three_bad_tokens(terminal):
    answers, _ = terminal
    answers += ["sk-ant-oat01-short", "sk-ant-api03-" + "a" * 90, "not a token"]
    assert setup.init() == 1 and not core.get_secret(core.TOKEN)


@pytest.mark.parametrize(
    "token, ok",
    [(TOKEN, True), ("sk-ant-oat01-" + "Ab9_-" * 16, True), ("sk-ant-oat01-" + "a" * 40, False),
     ("sk-ant-api03-" + "a" * 90, False), (TOKEN + "!", False)],
)  # fmt: skip
def test_the_claude_token_format(token, ok):
    assert bool(setup.TOKEN_RE.fullmatch(token)) == ok


def settings_file(home: Path) -> Path:
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_install_merges_the_rules_into_the_users_settings(isolated):
    path = settings_file(isolated)
    allow = ["Bash(ls:*)", "Bash(crab-student dashboard:*)"]  # (the second: an older crab-student's rule)
    deny = ["Read(.env)", "Write(~/.crab-student/config.json)"]  # (likewise)
    perms = {"allow": allow, "ask": ["Bash(rm:*)"], "deny": deny}
    path.write_text(json.dumps({"model": "opus", "permissions": perms}))
    setup.install_claude_files()
    setup.install_claude_files()
    settings = json.loads(path.read_text())
    assert settings["model"] == "opus" and settings["permissions"]["deny"] == ["Read(.env)", *setup.DENY]
    assert settings["permissions"]["allow"] == ["Bash(ls:*)", *setup.ALLOW]
    assert settings["permissions"]["ask"] == ["Bash(rm:*)", *setup.ASK]
    skill = isolated / ".claude" / "skills" / "crab-student" / "SKILL.md"
    assert skill.read_text(encoding="utf-8") == (setup.DATA / "SKILL.md").read_text(encoding="utf-8")


def test_settings_that_are_not_json_are_left_alone(isolated):
    path = settings_file(isolated)
    path.write_text('{"permissions": {"allow": [}')
    setup.install_claude_files()
    assert path.read_text() == '{"permissions": {"allow": [}'


INCLUDE = "Include ~/.crab-student/ssh/config"


def test_the_ssh_include_goes_first_once(isolated):
    config = isolated / ".ssh" / "config"
    assert setup.ensure_ssh_include()  # (no config yet)
    assert config.read_text() == INCLUDE + "\n"
    assert not setup.ensure_ssh_include()
    config.write_text("Host box\n  User me\n")
    assert setup.ensure_ssh_include()
    assert not setup.ensure_ssh_include()
    assert config.read_text().splitlines() == [INCLUDE, "Host box", "  User me"]


def test_an_include_further_down_is_moved_first(isolated):
    config = isolated / ".ssh" / "config"
    config.parent.mkdir()
    config.write_text(f"Host box\n  User me\n{INCLUDE}\n")  # (there, it falls inside the Host block)
    assert setup.ensure_ssh_include()
    assert config.read_text().splitlines() == [INCLUDE, "Host box", "  User me"]


def prefixes(rules: list[str]) -> list[str]:
    assert all(re.fullmatch(r"Bash\(crab-student [a-z ]+(:\*| \*--[a-z]+\*)\)", r) for r in rules), rules
    return [re.sub(r"(:\*| \*--[a-z]+\*)\)$", "", r[len("Bash(") :]) for r in rules]


def matches(rule: str, line: str) -> bool:
    """Claude Code's matching of a Bash rule: `:*` at the end is a prefix, `*` elsewhere any text."""
    pattern = rule[len("Bash(") : -1]
    if pattern.endswith(":*"):
        return line.startswith(pattern[:-2])
    return re.fullmatch(".*".join(map(re.escape, pattern.split("*"))), line) is not None


def test_the_permission_rules():
    # Spending beyond the gate, forcing a delete, changing settings or secrets, and copying a machine's files
    # into a folder of the caller's choice always ask (an ask rule wins over an allow rule); the launcher, a
    # plain delete (it refuses to lose work) and a fetch into its own new folder need no prompt.
    ask_first = ["approve job1", "delete job1 --force", "delete --force job1", "config idle_minutes 5", "init",
                 "secret set HF_TOKEN", "secret delete HF_TOKEN", "fetch job1 --to data/job1",
                 "fetch --to ~/.claude/skills/x job1"]  # fmt: skip
    allowed = ["guide launch", "offers --gpus 1", "list --json", "secret list", "launch job1 --hours 2",
               "start job1 --json", "status job1 --json", "delete job1", "decline job1", "wait --json",
               "wait job1 --timeout 90", "fetch job1 --json", "spend"]  # fmt: skip
    assert not any("extend" in rule for rule in setup.ALLOW + setup.ASK)  # (there is no time limit)
    for command in ask_first:
        assert any(matches(r, f"crab-student {command}") for r in setup.ASK), command
    for command in allowed:
        line = f"crab-student {command}"
        assert any(matches(r, line) for r in setup.ALLOW), command
        assert not any(matches(r, line) for r in setup.ASK), command


def test_a_flag_that_asks_first_is_not_taken_in_a_shorter_form():
    """(The ask rules look for the whole word: `delete job1 --f` would force a delete without asking.)"""
    parser = cli.build_parser()
    asking = [re.fullmatch(r"Bash\(crab-student ([a-z]+) \*(--[a-z]+)\*\)", rule) for rule in setup.ASK]
    flags = [m.groups() for m in asking if m]
    assert ("delete", "--force") in flags and ("fetch", "--to") in flags
    for command, flag in flags:
        for short in (flag[:n] for n in range(3, len(flag))):
            assert short in parser.parse_known_args([command, "job1", short])[1], (command, short)


def test_the_skill_and_the_launch_guide():
    skill = (setup.DATA / "SKILL.md").read_text(encoding="utf-8")
    head = skill.split("---\n")
    assert head[0] == "" and re.search(r"^name: crab-student$", head[1], re.M)
    description = re.search(r"^description: (.+)$", head[1], re.M).group(1)
    assert len(description) > 100 and len(skill) < 10000  # (the description says when to use it)
    raw = (setup.DATA / "launch.md").read_text(encoding="utf-8")
    guide = setup.guide("launch")
    assert len(raw) < 12000 and "{{" not in guide
    assert skill.isascii() and guide.isascii()  # (printed as ASCII: nothing may be lost)
    with pytest.raises(core.CrabError) as e:
        setup.guide("dashboard")
    assert e.value.code == 2


def test_the_markdown_and_the_rules_name_only_real_commands_and_flags():
    parser = cli.build_parser()
    subs = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
    skill = (setup.DATA / "SKILL.md").read_text(encoding="utf-8")
    guide = (setup.DATA / "launch.md").read_text(encoding="utf-8")
    for command in re.findall(r"`crab-student ([a-z]+)", skill + guide):
        assert command in subs, command
    for rule in prefixes(setup.ALLOW + setup.ASK):
        assert rule.split()[1] in subs, rule
    launcher_flags = set().union(*(subs[c]._option_string_actions for c in ("offers", "launch", "start")))
    assert set(re.findall(r"--[a-z][a-z-]+", guide)) <= launcher_flags
    every_flag = set().union(*(p._option_string_actions for p in subs.values()))
    assert set(re.findall(r"--[a-z][a-z-]+", skill)) <= every_flag


def test_the_guide_says_where_the_user_is_and_never_the_approval_price():
    raw = (setup.DATA / "launch.md").read_text(encoding="utf-8")
    assert "{{home}}" in raw and "approve_above_usd_per_hour" not in raw
    core.set_config("approve_above_usd_per_hour", "7.25")
    assert "{{" not in setup.guide("launch") and "7.25" not in setup.guide("launch")
    core.set_config("home", "ca")
    assert "the user is in CA" in setup.guide("launch")


def test_the_skill_starts_a_fork_that_follows_the_guide():
    skill = (setup.DATA / "SKILL.md").read_text(encoding="utf-8")
    assert 'subagent_type: "fork"' in skill and "`crab-student guide launch`" in skill
    assert not re.search(r"\$\d", skill)  # (the approval price is a setting, not a number in the text)
    assert not (setup.DATA / "launcher.md").exists()
