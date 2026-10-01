"""The local side of talking to machines, with no real ssh: the ssh config and host keys (shared by agents
brought up in threads), addresses, the bundle, what remote() makes of ssh's answers, the upload's progress
and failures, and opening terminals and VS Code."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import pytest

from crab_student import core, machine
from crab_student.machine import open_launch_windows  # (the real one: conftest replaces it for every test)


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


def hosts_in(config: str) -> list[str]:
    return [line.split()[1] for line in config.splitlines() if line.startswith("Host ")]


def together(fn, calls: list[tuple]) -> None:
    """Call fn(*args) for every args in `calls`, each in its own thread, all set off at the same moment;
    whatever a call raised is raised here."""
    go = threading.Barrier(len(calls), timeout=10)

    def call(args: tuple) -> None:
        go.wait()
        fn(*args)

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        for future in [pool.submit(call, args) for args in calls]:
            future.result()


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
    """subprocess.Popen for the upload: stdout gives `lines`, what goes to stdin is kept."""

    def __init__(self, lines: list[bytes], returncode: int = 0):
        self.stdin, self.stdout, self.stderr = Sink(), io.BytesIO(b"\n".join(lines) + b"\n"), io.BytesIO()
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        pass


# ---- the ssh config, host keys and addresses -----------------------------------------------------


def test_the_ssh_config_has_a_host_for_each_agent_with_an_address():
    core.save({"name": "job1", "created": "1", "ssh": {"host": "203.0.113.7", "port": 40122}})
    core.save({"name": "job2", "created": "2", "ssh": None})
    machine.write_ssh_config()
    text = machine.config_file().read_text()
    hosts = [line.split()[1] for line in text.splitlines() if line.startswith("Host ")]
    assert hosts == ["crab-student-job1"]
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


def test_a_vscode_settings_file_with_comments_is_edited_line_by_line(isolated):
    path = core.vscode_settings_file()
    path.parent.mkdir(parents=True)
    path.write_text(
        '{\n    // my comment\n    "remote.SSH.remotePlatform": {\n        "crab-student-gone": "linux",\n'
        '        "my-box": "linux"\n    },\n}\n'
    )
    core.save({"name": "job1", "created": "1"})
    machine.remember_linux_host("job1")
    text = path.read_text()
    assert "// my comment" in text and '"my-box": "linux"' in text and "crab-student-gone" not in text
    assert '"crab-student-job1": "linux",' in text
    machine.remember_linux_host("job1")
    assert path.read_text() == text  # (once)
    path.write_text('{\n    // only a comment\n    "editor.tabSize": 2\n}\n')
    machine.remember_linux_host("job1")
    assert '"remote.SSH.remotePlatform": {"crab-student-job1": "linux"},' in path.read_text()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_a_settings_file_with_comments_keeps_its_line_endings_and_last_line(isolated, newline):
    path = core.vscode_settings_file()
    path.parent.mkdir(parents=True)
    lines = ["{", "    // c", '    "remote.SSH.remotePlatform": {', '        "box": "linux"', "    }", "}"]
    path.write_bytes(newline.join(lines).encode())  # (no newline at the end)
    core.save({"name": "job1", "created": "1"})
    machine.remember_linux_host("job1")
    want = [*lines[:3], '        "crab-student-job1": "linux",', *lines[3:]]
    assert path.read_bytes() == newline.join(want).encode()


def test_agents_coming_up_in_threads_all_end_up_in_the_ssh_config():
    def come_up(i: int) -> None:
        ssh = {"host": "203.0.113.7", "port": 40000 + i}
        core.save({"name": f"job{i:02}", "created": f"{i:02}", "ssh": ssh})
        machine.write_ssh_config()

    together(come_up, [(i,) for i in range(16)])
    config = machine.config_file()
    text = config.read_text()
    assert hosts_in(text) == [machine.alias(f"job{i:02}") for i in range(16)]
    machine.write_ssh_config()  # (quietly, from the same records)
    assert config.read_text() == text
    assert [p.name for p in config.parent.iterdir() if p != config] == []  # (no temporary file left)


def test_an_agent_saved_while_another_thread_writes_the_ssh_config_is_not_left_out(monkeypatch):
    def add(name: str) -> None:
        core.save({"name": name, "created": name, "ssh": {"host": "203.0.113.7", "port": 40122}})

    add("job1")
    read_records = core.all_records
    job1_read, job2_written = threading.Event(), threading.Event()

    def slow_first_read() -> list[dict]:
        recs = read_records()
        if not job1_read.is_set():
            job1_read.set()
            job2_written.wait(0.1)  # (time enough for job2's write, unless the lock keeps it out)
        return recs

    monkeypatch.setattr(core, "all_records", slow_first_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(machine.write_ssh_config)
        assert job1_read.wait(10)
        add("job2")
        machine.write_ssh_config()
        job2_written.set()
        first.result(10)
    assert hosts_in(machine.config_file().read_text()) == [machine.alias("job1"), machine.alias("job2")]


def test_host_keys_are_forgotten_one_at_a_time(monkeypatch):
    core.ssh_dir().mkdir(parents=True)
    (core.ssh_dir() / "known_hosts").write_text("")
    guard, running, most, forgotten = threading.Lock(), 0, 0, []

    def keygen(cmd, **kw):  # (ssh-keygen -R rewrites the shared file: two at once would lose changes)
        nonlocal running, most
        with guard:
            running += 1
            most = max(most, running)
            forgotten.append(cmd[cmd.index("-R") + 1])
        time.sleep(0.01)
        with guard:
            running -= 1

    monkeypatch.setattr(core, "run", keygen)
    together(machine.forget_host_key, [(f"job{i}",) for i in range(8)])
    assert most == 1
    assert sorted(forgotten) == [machine.alias(f"job{i}") for i in range(8)]


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
    lines = [b"step 1/5 environment", b"Cloning into 'proj'...", b"step 2/5 repository",
             b'{"ok": false, "step": 2, "label": "repository", "error": "clone failed"}']  # fmt: skip
    proc, progress = Upload(lines, returncode=1), []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: proc)
    e = fails(5, machine.upload_and_setup, "job1", b"BUNDLE", progress.append)
    assert progress == ["step 1/5 environment", "step 2/5 repository"] and proc.stdin.getvalue() == b"BUNDLE"
    assert "repository" in e.message and "clone failed" in e.message


@pytest.mark.parametrize(
    "last,returncode,outcome",
    [
        (b'{"ok": true}', 0, "ok"),
        (b"step 3/5 agent settings", 255, "unreachable"),  # (the connection dropped: the launch can go on)
        (b"Traceback (most recent call last):", 1, "unreachable"),  # (no result: start can check and go on)
    ],
)
def test_the_upload_outcome(last, returncode, outcome, monkeypatch):
    monkeypatch.setattr(
        subprocess, "Popen", lambda cmd, **kw: Upload([b"step 1/5 environment", last], returncode)
    )
    try:
        machine.upload_and_setup("job1", b"BUNDLE", lambda line: None)
        got = "ok"
    except machine.Unreachable:
        got = "unreachable"
    except core.CrabError as e:
        got = e.code
    assert got == outcome


# ---- terminals -----------------------------------------------------------------------------------


def test_open_without_a_terminal_asks_for_one(monkeypatch):
    monkeypatch.setattr(machine, "interactive", lambda: False)
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


def test_a_racing_machine_is_checked_at_its_address_without_keeping_its_host_key(monkeypatch):
    ran = []
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0)
    )
    assert machine.reachable("job1", ("203.0.113.9", 41022))
    ((_, *argv),) = ran
    assert "root@203.0.113.9" in argv and argv[argv.index("-p") + 1] == "41022" and argv[-1] == "true"
    assert machine.alias("job1") not in argv  # (only the winner's host key is kept, under the alias)
    known = next(a.split("=", 1)[1] for a in argv if a.startswith("UserKnownHostsFile="))
    assert known != str(core.ssh_dir() / "known_hosts").replace("\\", "/")


@pytest.mark.parametrize("system", ["windows", "macos", "linux", "windows without wt"])
def test_launch_windows_follow_each_agent(system, monkeypatch):
    monkeypatch.setattr(machine.os, "name", "nt" if system.startswith("windows") else "posix")
    monkeypatch.setattr(machine.sys, "platform", {"macos": "darwin", "linux": "linux"}.get(system, "win32"))
    monkeypatch.setattr(core, "tool", lambda name: None if system == "windows without wt" else f"/bin/{name}")
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: started.append(argv))
    opened = open_launch_windows(["job1", "job2"])
    assert opened is (system in ("windows", "macos"))
    if system == "windows":  # one window, a tab per agent, each following its launch with open
        ((wt, *argv),) = started
        tabs = [g.split("\0") for g in "\0".join(argv).split("\0;\0")]
        assert wt == "/bin/wt" and all(
            t[-2:] == ["open", n] and "new-tab" in t for t, n in zip(tabs, ["job1", "job2"], strict=True)
        )
    elif system == "macos":  # a Terminal window per agent
        scripts = [argv[-1] for argv in started]
        assert [argv[0] for argv in started] == ["/bin/osascript"] * 2
        assert "open job1" in scripts[0] and "open job2" in scripts[1]
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


def test_open_vscode_types_an_interactive_agents_task_into_the_claude_panel(machines, vscode):
    brief = "Fix the flaky test in tests/test_io.py & get CI to 100% green.\n\nUse pytest -x + the logs."
    machines.replies[("job1", "status")] = {"frontend": "vscode"}
    assert machine.open_vscode(agent_record(brief=brief)) == 0
    assert vscode["code"] == [
        ["/usr/bin/code", "--remote", "ssh-remote+crab-student-job1", "/root/work/proj"]
    ]
    [url] = vscode["urls"]
    assert panel_prompt(url) == brief.strip()


@pytest.mark.parametrize("mode, status, panel", [
    ("interactive", {"frontend": "tmux"}, False),  # (handed off since the record was written)
    ("colleague", {"frontend": "tmux"}, False),
    ("interactive", machine.Unreachable("no answer"), True),  # (the machine cannot say: the record does)
    ("colleague", machine.Unreachable("no answer"), False),
])  # fmt: skip
def test_open_vscode_opens_the_claude_panel_only_while_claude_works_there(
    machines, vscode, mode, status, panel
):
    machines.replies[("job1", "status")] = status
    assert machine.open_vscode(agent_record(mode, brief="Train the model.")) == 0
    assert len(vscode["code"]) == 1 and bool(vscode["urls"]) == panel


def test_a_brief_too_long_for_the_panel_is_pointed_to_instead(machines, vscode):
    machines.replies[("job1", "status")] = {"frontend": "vscode"}
    machine.open_vscode(agent_record(brief="Train the model. " * 400))
    assert "/root/BRIEF.md" in panel_prompt(vscode["urls"][0])


def test_open_vscode_without_the_code_command_says_how_to_get_it(monkeypatch, vscode):
    monkeypatch.setattr(core, "tool", lambda name: None)
    assert "'code' command" in fails(1, machine.open_vscode, agent_record()).message
    assert vscode == {"code": [], "urls": []}


def test_the_panel_says_when_the_dependencies_are_still_being_installed(machines, vscode):
    machines.replies[("job1", "status")] = {
        "frontend": "vscode",
        "deps": {"state": "running", "command": "uv sync"},
    }
    machine.open_vscode(agent_record(brief="Train the model."))
    prompt = panel_prompt(vscode["urls"][0])
    assert prompt.startswith("Train the model.") and "uv sync" in prompt and "agent-status" in prompt


def test_open_vscode_on_an_agent_whose_status_is_known_does_not_ask_the_machine_again(machines, vscode):
    machine.open_vscode(agent_record(brief="Train the model."), {"frontend": "vscode"})
    assert machines.calls == [] and vscode["urls"]


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


def test_the_wait_for_a_hand_off_ends_when_the_agent_runs_in_tmux(monkeypatch, capsys):
    answers = [{"frontend": "vscode"}, {"frontend": "tmux"}]
    monkeypatch.setattr(machine, "remote", lambda name, args, **kw: answers.pop(0))
    assert (
        machine.wait_for_handoff("job1") is True and answers == [] and "/handoff" in capsys.readouterr().out
    )


@pytest.mark.parametrize("answer", [machine.Unreachable("no answer"), KeyboardInterrupt()])
def test_the_wait_for_a_hand_off_ends_when_the_machine_cannot_be_reached_or_on_ctrl_c(monkeypatch, answer):
    def remote(name, args, **kw):
        raise answer

    monkeypatch.setattr(machine, "remote", remote)
    assert machine.wait_for_handoff("job1") is False
