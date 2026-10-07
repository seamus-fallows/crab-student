"""The local side of talking to machines, with no real ssh: the ssh config, addresses, the bundle, what remote()
makes of ssh's answers, the upload's progress and failures, and opening terminals and VS Code."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import urllib.parse
from pathlib import Path

import pytest

from crab_student import core, machine
from crab_student.machine import open_launch_window  # (the real one: conftest replaces it for every test)


@pytest.fixture(autouse=True)
def tools(monkeypatch):
    """ssh is installed; Windows Terminal is not."""
    monkeypatch.setattr(core, "tool", lambda name: None if name == "wt" else f"/usr/bin/{name}")


def fails(code: int, fn, *a) -> core.CrabError:
    with pytest.raises(core.CrabError) as e:
        fn(*a)
    assert e.value.code == code, e.value.message
    return e.value


def unpack(data: bytes) -> dict[str, tuple[bytes, int]]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {m.name: (tar.extractfile(m).read(), m.mode) for m in tar.getmembers()}


def fake_run(monkeypatch, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"", raises=None) -> list:
    """subprocess.run answering the way ssh would; returns the calls made (command, stdin)."""
    calls = []

    def run(cmd, input=None, **kw):
        calls.append((cmd, input))
        if raises:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


class Sink(io.BytesIO):
    def close(self) -> None:  # (keeps what was written readable)
        pass


class Upload:
    """subprocess.Popen for the upload: stdout gives `lines` (then, with `hang`, nothing more until it is
    killed), what goes to stdin is kept."""

    def __init__(self, lines: list[bytes], returncode: int = 0, hang: bool = False):
        self.stdin, self.stderr, self.killed = Sink(), io.BytesIO(), threading.Event()
        self.stdout = self.output([line + b"\n" for line in lines], hang)
        self.returncode = returncode

    def output(self, lines: list[bytes], hang: bool):
        yield from lines
        if hang:
            self.killed.wait(30)

    def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed.set()


# ---- the ssh config, host keys and addresses -----------------------------------------------------


def test_the_ssh_config_has_a_host_for_each_agent_with_an_address():
    core.save({"name": "job1", "created": "1", "ssh": {"host": "203.0.113.7", "port": 40122}})
    core.save({"name": "job2", "created": "2", "ssh": None})
    machine.write_ssh_config()
    config = machine.config_file()
    text = config.read_text()
    hosts = [line.split()[1] for line in text.splitlines() if line.startswith("Host ")]
    assert hosts == ["crab-student-job1"]
    assert [p.name for p in config.parent.iterdir() if p != config] == []  # (no temporary file left)
    for line in ("HostName 203.0.113.7", "Port 40122", "User root", "HostKeyAlias crab-student-job1"):
        assert f"  {line}\n" in text
    # (paths under the home folder as "~/...": Git's ssh, which VS Code may run, reads "C:/..." as relative)
    assert "  IdentityFile ~/.crab-student/ssh/id_ed25519\n" in text
    assert "  UserKnownHostsFile ~/.crab-student/ssh/known_hosts\n" in text and "\\" not in text


def test_opening_in_vscode_marks_the_host_as_linux_and_forgets_agents_that_are_gone(isolated, monkeypatch):
    path = core.vscode_settings_file()
    path.parent.mkdir(parents=True)
    old = {"crab-student-gone": "linux", "my-box": "linux"}
    path.write_text(json.dumps({"editor.tabSize": 2, machine.REMOTE_PLATFORM: old}))
    core.save({"name": "job1", "created": "1"})
    machine.remember_linux_host("job1")
    settings = json.loads(path.read_text())
    assert settings[machine.REMOTE_PLATFORM] == {"my-box": "linux", "crab-student-job1": "linux"}
    assert settings["editor.tabSize"] == 2


def test_a_vscode_settings_file_with_comments_is_left_alone(isolated):
    path = core.vscode_settings_file()
    path.parent.mkdir(parents=True)
    raw = (
        b'{\r\n    // my comment\r\n    "remote.SSH.remotePlatform": {\r\n        "crab-student-gone": "linux",'
        b'\r\n        "my-box": "linux"\r\n    },\r\n}'
    )
    path.write_bytes(raw)
    core.save({"name": "job1", "created": "1"})
    machine.remember_linux_host("job1")
    assert path.read_bytes() == raw and [p.name for p in path.parent.iterdir()] == ["settings.json"]


def test_set_address_follows_the_direct_port_and_says_when_it_moved():
    rec = {"ssh": None}
    inst = {"public_ipaddr": "203.0.113.7", "ports": {"22/tcp": [{"HostPort": "40122"}]}}
    assert machine.set_address(rec, inst) and rec["ssh"] == {"host": "203.0.113.7", "port": 40122}
    assert not machine.set_address(rec, inst)
    assert not machine.set_address(rec, {"public_ipaddr": ""}) and rec["ssh"]["port"] == 40122  # (none: kept)
    inst["ports"]["22/tcp"][0]["HostPort"] = "40999"
    assert machine.set_address(rec, inst) and rec["ssh"]["port"] == 40999


# ---- the bundle ----------------------------------------------------------------------------------


def test_the_bundle_sets_modes_and_lf_endings_and_skips_dotfiles_and_folders(tmp_path, monkeypatch):
    src = tmp_path / "remote"
    (src / "sub").mkdir(parents=True)
    (src / "cs.py").write_bytes(b"import os\r\nprint(1)\r\n")
    (src / "rules.md").write_bytes(b"# rules\r\n")
    (src / ".hidden").write_bytes(b"x")
    (src / "sub" / "inner.py").write_bytes(b"x")
    monkeypatch.setattr(machine, "REMOTE_SRC", src)
    files = unpack(machine.bundle({"claude.env": (b"export T=1\n", 0o600), "BRIEF.md": (b"brief\n", 0o644)}))
    assert files == {
        "cs.py": (b"import os\nprint(1)\n", 0o755),
        "rules.md": (b"# rules\n", 0o644),
        "claude.env": (b"export T=1\n", 0o600),
        "BRIEF.md": (b"brief\n", 0o644),
    }


def test_the_real_bundle_carries_the_machine_code_with_lf_endings():
    files = unpack(machine.bundle({}))
    modes = {name: mode for name, (_, mode) in files.items()}
    assert modes == {"cs.py": 0o755, "rules.md": 0o644, "rules-autonomous.md": 0o644,
                     "rules-colleague.md": 0o644, "handoff.md": 0o644}  # fmt: skip
    assert not any(b"\r\n" in data for data, _ in files.values()) and not any("/" in name for name in files)
    assert (
        unpack(machine.bundle({}, fake_claude=True))["fake_claude.py"][1] == 0o755
    )  # (only when it is used)


# ---- remote --------------------------------------------------------------------------------------


def test_remote_sends_stdin_and_returns_the_last_json_line(monkeypatch):
    calls = fake_run(monkeypatch, stdout=b'Welcome to your machine\n{"note": 1}\n{"sent": true}\n')
    assert machine.remote("job1", ["send"], input=b"hello") == {"sent": True}
    ((cmd, stdin),) = calls
    assert stdin == b"hello" and machine.alias("job1") in cmd and cmd[-1].endswith("cs.py send")


@pytest.mark.parametrize(
    "answer,unreachable,said",
    [
        (
            {"returncode": 255, "stderr": b"ssh: connect to host 203.0.113.7: Connection timed out\n"},
            True,
            "",
        ),
        ({"raises": subprocess.TimeoutExpired("ssh", 60)}, True, ""),
        (
            {"returncode": 1, "stdout": b'{"error": "there is no repository yet"}\n'},
            False,
            "no repository yet",
        ),
        ({"stdout": b"not json\n"}, False, ""),
    ],
)
def test_remote_failures(answer, unreachable, said, monkeypatch):
    fake_run(monkeypatch, **answer)
    with pytest.raises(core.CrabError) as e:
        machine.remote("job1", ["status"])
    assert isinstance(e.value, machine.Unreachable) is unreachable and said in e.value.message


# ---- the upload ----------------------------------------------------------------------------------


def test_the_upload_streams_setup_progress_and_a_failed_step_is_exit_5(monkeypatch):
    lines = [b"step 1/6 environment", b"Cloning into 'proj'...", b"step 2/6 repository",
             b'{"ok": false, "step": 2, "label": "repository", "error": "clone failed"}']  # fmt: skip
    proc, progress = Upload(lines, returncode=1), []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: proc)
    e = fails(5, machine.upload_and_setup, "job1", b"BUNDLE", progress.append)
    assert progress == ["step 1/6 environment", "step 2/6 repository"] and proc.stdin.getvalue() == b"BUNDLE"
    assert "repository" in e.message and "clone failed" in e.message


@pytest.mark.parametrize(
    "last,returncode,hang,outcome",
    [
        (b'{"ok": true}', 0, False, "ok"),
        (b"step 3/6 dependencies", 255, False, "unreachable"),  # (ssh lost the connection: it goes on)
        (b"step 3/6 dependencies", 1, True, "unreachable"),  # (the timeout killed ssh: 1 on Windows)
        (b"Traceback (most recent call last):", 1, False, 5),  # (setup failed before its result)
        (b"sh: 1: exec: ./python: not found", 127, False, 5),
    ],
)
def test_the_upload_outcome(last, returncode, hang, outcome, monkeypatch):
    monkeypatch.setattr(
        subprocess, "Popen", lambda cmd, **kw: Upload([b"step 1/6 environment", last], returncode, hang)
    )
    try:
        machine.upload_and_setup("job1", b"BUNDLE", lambda line: None, timeout=0.2 if hang else 60)
        got = "ok"
    except machine.Unreachable:
        got = "unreachable"
    except core.CrabError as e:
        got = e.code
    assert got == outcome


def fake_python(path: Path, version: tuple[int, int]) -> Path:
    """A stand-in Python of `version`: it answers setup's version check, else says how it was run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\n[ "$1" = -c ] && exit {int(version < machine.MIN_PYTHON)}\necho "$0 $*"\n')
    path.chmod(0o755)
    return path


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell")
@pytest.mark.parametrize("version, runs", [((3, 11), True), ((3, 13), True), ((3, 10), False)])
def test_setup_runs_on_the_first_new_enough_python_and_links_it_for_what_comes_after(tmp_path, version, runs):
    if Path("/venv/main/bin/python").exists():
        pytest.skip("this computer has the image's /venv/main")
    python3 = fake_python(tmp_path / "bin" / "python3", version)
    d = tmp_path / "crab-student"
    d.mkdir()
    env = {"PATH": f"{python3.parent}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    p = subprocess.run(
        ["sh", "-c", machine.SETUP], cwd=d, env=env, capture_output=True, text=True, timeout=30
    )
    if runs:
        program, *args = p.stdout.split()
        assert p.returncode == 0 and Path(program).name == "python" and args == ["cs.py", "setup"]
        assert (d / "python").resolve() == python3.resolve()
    else:  # (no Python new enough: a failed setup, exit 5 once it reaches the local side)
        assert p.returncode == 1 and not (d / "python").exists() and p.stderr


# ---- terminals -----------------------------------------------------------------------------------


def test_open_without_a_terminal_asks_for_one(monkeypatch):
    monkeypatch.setattr(core, "interactive", lambda: False)
    fails(2, machine.open_one, "job1")


def test_open_all_on_windows_makes_two_columns_of_equal_panes(monkeypatch):
    monkeypatch.setattr(machine.os, "name", "nt")
    monkeypatch.setattr(core, "tool", lambda name: f"C:/Tools/{name}.exe")
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: started.append(argv))
    assert machine.open_all(["a", "b", "c", "d", "e"]) == 0
    ((wt, *argv),) = started
    groups = [g.split("\0") for g in "\0".join(argv).split("\0;\0")]
    panes = [g for g in groups if g[-1] == "agent-attach"]
    # left column a, b, c; right column d, e; each column split into equal heights
    assert wt == "C:/Tools/wt.exe" and [p[p.index("--title") + 1] for p in panes] == ["a", "d", "b", "c", "e"]
    assert [p[p.index("--size") + 1] for p in panes if "--size" in p] == ["0.667", "0.500", "0.500"]
    assert sum("-V" in p for p in panes) == 1
    assert ["move-focus", "left"] in groups and ["move-focus", "right"] in groups


@pytest.mark.parametrize("system", ["windows", "macos", "linux", "windows without wt"])
def test_a_launch_window_follows_the_agent(system, monkeypatch):
    monkeypatch.setattr(machine.os, "name", "nt" if system.startswith("windows") else "posix")
    monkeypatch.setattr(machine.sys, "platform", {"macos": "darwin", "linux": "linux"}.get(system, "win32"))
    monkeypatch.setattr(core, "tool", lambda name: None if system == "windows without wt" else f"/bin/{name}")
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: started.append(argv))
    assert open_launch_window("job1") is (system in ("windows", "macos"))
    if system == "windows":  # a tab in the window named crab-student (launches side by side collect there)
        ((wt, *argv),) = started
        assert [wt, *argv[:5]] == ["/bin/wt", "-w", "crab-student", "new-tab", "--title", "job1"]
        assert argv[5:] == [sys.executable, "-m", "crab_student", "open", "job1"]
    elif system == "macos":  # a Terminal window
        ((osascript, *argv),) = started
        assert osascript == "/bin/osascript" and "open job1" in argv[-1]
    else:
        assert not started


# ---- VS Code -------------------------------------------------------------------------------------


@pytest.fixture
def vscode(monkeypatch) -> dict:
    """VS Code as open --vscode drives it: the `code` command lines run, and the URLs opened."""
    opened = {"code": [], "urls": []}
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: opened["code"].append(argv))
    monkeypatch.setattr(machine.webbrowser, "open", opened["urls"].append)
    monkeypatch.setattr(machine, "VSCODE_URL_DELAY", 0)
    return opened


def agent_record(mode: str = "interactive", brief: str = "") -> dict:
    if brief:
        core.agents_dir().mkdir(parents=True, exist_ok=True)
        (core.agents_dir() / "job1.brief.md").write_text(brief, encoding="utf-8")
    return {"name": "job1", "mode": mode, "repo": {"dir": "/root/work/proj"}}


def panel_prompt(url: str) -> str:
    base, _, query = url.partition("?")
    assert base == "vscode://anthropic.claude-code/open"
    return urllib.parse.parse_qs(query)["prompt"][0]


def test_open_vscode_types_an_interactive_agents_task_into_the_claude_panel(vscode):
    brief = "Fix the flaky test in tests/test_io.py & get CI to 100% green.\n\nUse pytest -x + the logs."
    assert machine.open_vscode(agent_record(brief=brief), panel=True) == 0
    assert vscode["code"] == [
        ["/usr/bin/code", "--remote", "ssh-remote+crab-student-job1", "/root/work/proj"]
    ]
    [url] = vscode["urls"]
    assert panel_prompt(url) == brief.strip()
    assert machine.open_vscode(agent_record("colleague", brief=brief), panel=False) == 0
    assert len(vscode["code"]) == 2 and len(vscode["urls"]) == 1  # (Claude works in tmux: no panel)


@pytest.mark.parametrize("mode, status, works_there", [
    ("interactive", {"frontend": "vscode"}, True),
    ("interactive", {"frontend": "tmux"}, False),  # (handed off since the record was written)
    ("colleague", {"frontend": "tmux"}, False),
    ("interactive", machine.Unreachable("no answer"), True),  # (the machine cannot say: the record does)
    ("colleague", machine.Unreachable("no answer"), False),
])  # fmt: skip
def test_claude_works_in_vs_code_as_the_machine_says_else_as_the_record_says(
    machines, mode, status, works_there
):
    machines.replies[("job1", "status")] = status
    assert machine.in_vscode(agent_record(mode)) is works_there


def test_a_brief_too_long_for_the_panel_is_pointed_to_instead(vscode):
    machine.open_vscode(agent_record(brief="Train the model. " * 400), panel=True)
    assert "/root/BRIEF.md" in panel_prompt(vscode["urls"][0])


def test_what_open_vscode_prints_is_ascii(vscode, monkeypatch, tmp_path, capsys):
    settings = (
        tmp_path / "Zoë" / "settings.json"
    )  # (VS Code's settings under a home folder named outside ASCII)
    settings.parent.mkdir()
    settings.write_text("{\n    // a comment\n}\n", encoding="utf-8")
    monkeypatch.setattr(core, "vscode_settings_file", lambda: settings)
    rec = {"name": "job1", "mode": "interactive", "repo": {"dir": "/root/work/café"}}
    assert machine.open_vscode(rec, panel=False) == 0
    out = capsys.readouterr().out
    assert out.isascii() and out.count("\n") == 2  # (the platform it will ask about, and what opens)


def test_open_vscode_without_the_code_command_says_how_to_get_it(monkeypatch, vscode):
    monkeypatch.setattr(core, "tool", lambda name: None)
    assert "'code' command" in fails(1, machine.open_vscode, agent_record(), True).message
    assert vscode == {"code": [], "urls": []}


def test_the_users_vs_code_commit_goes_to_the_machine(monkeypatch):
    commit = "0123456789abcdef0123456789abcdef01234567"
    fake_run(monkeypatch, stdout=f"1.105.0\n{commit}\nx64\n")
    assert machine.local_vscode() == commit


@pytest.mark.parametrize("answer", ["", "1.105.0\nnot a commit\n", OSError("no such file")])
def test_without_a_usable_vs_code_the_machine_prepares_nothing_for_it(monkeypatch, answer):
    if isinstance(answer, Exception):
        fake_run(monkeypatch, raises=answer)
    else:
        fake_run(monkeypatch, stdout=answer)
    assert machine.local_vscode() is None
    monkeypatch.setattr(core, "tool", lambda name: None)
    assert machine.local_vscode() is None


def test_the_wait_for_a_hand_off_asks_the_machines_status_until_the_agent_runs_in_tmux(monkeypatch):
    answers = [{"frontend": "vscode"}, {"frontend": "vscode"}, {"frontend": "tmux"}]
    asked, slept = [], []

    def remote(name, args, **kw):
        asked.append((name, args))
        return answers.pop(0)

    monkeypatch.setattr(machine, "remote", remote)
    monkeypatch.setattr(machine.time, "sleep", slept.append)
    assert machine.wait_for_handoff("job1") is True
    assert asked == [("job1", ["status"])] * 3 and slept == [machine.HANDOFF_POLL] * 2 and answers == []


def fake_status(monkeypatch, answers: list) -> None:
    """machine.remote answering from `answers` (an exception is raised); no sleeping."""

    def remote(name, args, **kw):
        reply = answers.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(machine, "remote", remote)
    monkeypatch.setattr(machine.time, "sleep", lambda seconds: None)


def test_the_wait_for_a_hand_off_rides_out_a_short_loss_of_the_machine(monkeypatch):
    lost = [machine.Unreachable("no answer")] * (machine.HANDOFF_MISSES - 1)
    answers = [{"frontend": "vscode"}, *lost, {"frontend": "vscode"}, *lost, {"frontend": "tmux"}]
    fake_status(monkeypatch, answers)
    assert machine.wait_for_handoff("job1") is True and answers == []


def test_the_wait_for_a_hand_off_ends_when_the_machine_is_lost_for_a_minute_or_on_ctrl_c(monkeypatch):
    answers = [{"frontend": "vscode"}, *[machine.Unreachable("no answer")] * machine.HANDOFF_MISSES]
    fake_status(monkeypatch, answers)
    assert machine.wait_for_handoff("job1") is False and answers == []
    answers = [{"frontend": "vscode"}, KeyboardInterrupt()]
    fake_status(monkeypatch, answers)
    assert machine.wait_for_handoff("job1") is False and answers == []
