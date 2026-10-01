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
    monkeypatch.setattr(setup, "claude_version", lambda: setup.FORK_VERSION)
    return answers, keygens


def test_init_asks_only_for_what_is_missing(terminal, fake_vast, isolated, capsys):
    answers, keygens = terminal
    core.vast_key_file().unlink()
    fake_vast.user["email"] = "adé@example.com"
    answers += ["wrong-key", fake_vast.key, "sk-ant-oat01-too-short", f"  {TOKEN}  "]
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


def test_init_suggests_the_home_setting_from_the_locale(terminal, ready, monkeypatch, capsys):
    monkeypatch.setenv("LC_ALL", "fr_CA.UTF-8")
    assert setup.guess_country() == "CA"
    setup.init()
    assert (
        "config home CA" in capsys.readouterr().out and core.load_config()["home"] == ""
    )  # (only suggested)
    core.set_config("home", "de")
    setup.init()
    assert "config home" not in capsys.readouterr().out
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setattr(setup.os, "name", "posix")
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    assert setup.guess_country() == ""


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


def test_install_removes_the_launcher_agent_of_older_versions(isolated):
    old = isolated / ".claude" / setup.OLD_LAUNCHER_AGENT
    old.parent.mkdir(parents=True)
    old.write_text("an older launcher")
    setup.install_claude_files()
    assert not old.exists()
    setup.install_claude_files()  # (nothing left to remove)


def test_settings_that_are_not_json_are_left_alone(isolated):
    path = settings_file(isolated)
    path.write_text('{"permissions": {"allow": [}')
    setup.install_claude_files()
    assert path.read_text() == '{"permissions": {"allow": [}'


def test_the_ssh_include_goes_first_once(isolated):
    config = isolated / ".ssh" / "config"
    config.parent.mkdir()
    config.write_text("Host box\n  User me\n")
    assert setup.ensure_ssh_include()
    assert not setup.ensure_ssh_include()
    first, *rest = config.read_text().splitlines()
    assert first.startswith("Include ") and (core.ssh_dir() / "config").as_posix() in first
    assert rest == ["Host box", "  User me"]


def prefixes(rules: list[str]) -> list[str]:
    assert all(re.fullmatch(r"Bash\(crab-student [a-z ]+:\*\)", r) for r in rules), rules
    return [r[len("Bash(") : -len(":*)")] for r in rules]


def test_the_permission_rules():
    allow, ask = prefixes(setup.ALLOW), prefixes(setup.ASK)
    # Spending beyond the gate, destroying and changing settings always ask; the launcher needs no prompt.
    ask_first = ["approve job1", "decline job1", "delete job1 --force", "config idle_minutes 5", "init",
                 "secret set HF_TOKEN", "secret delete HF_TOKEN"]  # fmt: skip
    launcher = ["guide launch", "offers --gpus 1", "list --json", "secret list", "launch job1 --hours 2",
                "start job1 --json", "status job1 --json"]  # fmt: skip
    assert not any("extend" in rule for rule in setup.ALLOW + setup.ASK)  # (there is no time limit)
    for command in ask_first:
        line = f"crab-student {command}"
        assert any(line.startswith(p) for p in ask), command
        assert not any(line.startswith(p) for p in allow), command  # (even as a bare string prefix)
    for command in launcher:
        line = f"crab-student {command}"
        assert any(line.startswith(p) for p in allow) and not any(line.startswith(p) for p in ask), command


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
    assert set(re.findall(r"--[a-z][a-z-]+", guide)) <= set(subs["launch"]._option_string_actions)
    every_flag = set().union(*(p._option_string_actions for p in subs.values()))
    assert set(re.findall(r"--[a-z][a-z-]+", skill)) <= every_flag


def test_the_guide_names_the_approval_price_from_the_settings():
    raw = (setup.DATA / "launch.md").read_text(encoding="utf-8")
    assert "{{approve_above_usd_per_hour}}" in raw and "$5/hr" in setup.guide("launch")
    core.set_config("approve_above_usd_per_hour", "7.5")
    assert "$7.5/hr" in setup.guide("launch")


def test_the_skill_starts_a_fork_that_follows_the_guide():
    skill = (setup.DATA / "SKILL.md").read_text(encoding="utf-8")
    assert 'subagent_type: "fork"' in skill and "`crab-student guide launch`" in skill
    assert not re.search(r"\$\d", skill)  # (the approval price is a setting, not a number in the text)
    assert not (setup.DATA / "launcher.md").exists()


# ---- the version check and VS Code -----------------------------------------------------------------


@pytest.mark.parametrize(
    "output, version",
    [("2.1.285 (Claude Code)\n", (2, 1, 285)), ("claude 2.1.9", (2, 1, 9)), ("no version here", None)],
)
def test_claude_version_is_read_from_its_output(output, version, monkeypatch):
    monkeypatch.setattr(core, "tool", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=output))
    assert setup.claude_version() == version


def test_an_older_claude_code_is_warned_about(monkeypatch, capsys):
    monkeypatch.setattr(setup, "claude_version", lambda: (2, 1, 100))
    setup._claude_version_step()
    assert "claude update" in capsys.readouterr().out
    monkeypatch.setattr(setup, "claude_version", lambda: setup.FORK_VERSION)
    setup._claude_version_step()
    assert "Warning" not in capsys.readouterr().out


@pytest.fixture
def vscode(isolated, monkeypatch, capsys):
    """VS Code's user settings in the temporary home; the test says what the user types (a list of answers)
    and whether it is a terminal. Returns (settings path, answers)."""
    path = setup.vscode_settings_file()
    path.parent.mkdir(parents=True)
    answers: list[str] = []

    def ask(prompt: str = "") -> str:
        assert answers, f"asked: {prompt}"
        return answers.pop(0)

    monkeypatch.setattr("builtins.input", ask)
    monkeypatch.setattr(setup, "interactive", lambda: True)
    return path, answers


def extensions(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8")).get(setup.VSCODE_EXTENSIONS)


def test_vscode_gets_the_claude_extension_on_machines_when_the_user_agrees(vscode):
    path, answers = vscode
    path.write_text(json.dumps({"editor.fontSize": 14}))
    answers.append("")
    setup._vscode_step()
    assert extensions(path) == [setup.CLAUDE_EXTENSION]
    assert json.loads(path.read_text())["editor.fontSize"] == 14
    setup._vscode_step()  # (already there: nothing is asked)


def test_vscode_extensions_the_user_already_lists_are_kept(vscode):
    path, answers = vscode
    path.write_text(json.dumps({setup.VSCODE_EXTENSIONS: ["ms-python.python"]}))
    answers.append("y")
    setup._vscode_step()
    assert extensions(path) == ["ms-python.python", setup.CLAUDE_EXTENSION]


def test_vscode_settings_are_created_when_there_are_none(vscode):
    path, answers = vscode
    answers.append("yes")
    setup._vscode_step()
    assert extensions(path) == [setup.CLAUDE_EXTENSION]


@pytest.mark.parametrize("how", ["declined", "not a terminal", "with comments", "not a list"])
def test_vscode_settings_are_left_alone_unless_the_edit_is_safe_and_agreed(how, vscode, monkeypatch, capsys):
    path, answers = vscode
    texts = {
        "with comments": '{\n  // my settings\n  "editor.fontSize": 14,\n}\n',
        "not a list": json.dumps({setup.VSCODE_EXTENSIONS: "anthropic.claude-code"}),
    }
    text = texts.get(how, "{}")
    path.write_text(text)
    if how == "declined":
        answers.append("n")
    if how == "not a terminal":
        monkeypatch.setattr(setup, "interactive", lambda: False)
    setup._vscode_step()
    assert path.read_text() == text
    assert setup.CLAUDE_EXTENSION in capsys.readouterr().out  # (it says what to add instead)


def test_without_vscode_nothing_is_created(isolated, capsys):
    setup._vscode_step()
    assert not setup.vscode_settings_file().parent.exists()
    assert capsys.readouterr().out.isascii()
