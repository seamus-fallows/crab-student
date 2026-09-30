"""The local side of talking to machines, with no real ssh: the ssh config, addresses, the bundle, what
remote() makes of ssh's answers, the upload's progress and failures, and opening terminals."""

from __future__ import annotations

import io
import subprocess
import tarfile

import pytest

from crab_student import core, machine


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
    """subprocess.Popen for the upload: stdout gives `lines`, what goes to stdin is kept."""

    def __init__(self, lines: list[bytes], returncode: int = 0):
        self.stdin, self.stdout, self.stderr = Sink(), io.BytesIO(b"\n".join(lines) + b"\n"), io.BytesIO()
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        pass


# ---- the ssh config and addresses ----------------------------------------------------------------


def test_the_ssh_config_has_a_host_for_each_worker_with_an_address():
    core.save({"name": "job1", "created": "1", "ssh": {"host": "203.0.113.7", "port": 40122}})
    core.save({"name": "job2", "created": "2", "ssh": None})
    machine.write_ssh_config()
    text = machine.config_file().read_text()
    hosts = [line.split()[1] for line in text.splitlines() if line.startswith("Host ")]
    assert hosts == ["crab-student-job1"]
    for line in ("HostName 203.0.113.7", "Port 40122", "User root", "HostKeyAlias crab-student-job1"):
        assert f"  {line}\n" in text
    key, known = (str(core.ssh_dir() / name).replace("\\", "/") for name in ("id_ed25519", "known_hosts"))
    assert f'IdentityFile "{key}"' in text and f'UserKnownHostsFile "{known}"' in text and "\\" not in text


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
    (src / "worker.md").write_bytes(b"# rules\r\n")
    (src / ".hidden").write_bytes(b"x")
    (src / "sub" / "inner.py").write_bytes(b"x")
    monkeypatch.setattr(machine, "REMOTE_SRC", src)
    files = unpack(machine.bundle({"claude.env": (b"export T=1\n", 0o600), "BRIEF.md": (b"brief\n", 0o644)}))
    assert files == {
        "cs.py": (b"import os\nprint(1)\n", 0o755),
        "worker.md": (b"# rules\n", 0o644),
        "claude.env": (b"export T=1\n", 0o600),
        "BRIEF.md": (b"brief\n", 0o644),
    }


def test_the_real_bundle_carries_the_machine_code_with_lf_endings():
    files = unpack(machine.bundle({}))
    modes = {name: mode for name, (_, mode) in files.items()}
    assert (modes["cs.py"], modes["fake_claude.py"], modes["worker.md"]) == (0o755, 0o755, 0o644)
    assert not any(b"\r\n" in data for data, _ in files.values()) and not any("/" in name for name in files)


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
        (b"step 3/5 worker settings", 255, "unreachable"),  # (the connection dropped: the launch can go on)
        (b"Traceback (most recent call last):", 1, 5),  # (setup died without a result)
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
    monkeypatch.setattr(machine, "_interactive", lambda: False)
    fails(2, machine.open_one, "job1")


def test_open_all_on_windows_makes_two_columns_of_equal_panes(monkeypatch):
    monkeypatch.setattr(machine.os, "name", "nt")
    monkeypatch.setattr(core, "tool", lambda name: f"C:/Tools/{name}.exe")
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: started.append(argv))
    assert machine.open_all(["a", "b", "c", "d", "e"]) == 0
    ((wt, *argv),) = started
    groups = [g.split("\0") for g in "\0".join(argv).split("\0;\0")]
    panes = [g for g in groups if g[-1] == "crab-attach"]
    # left column a, b, c; right column d, e; each column split into equal heights
    assert wt == "C:/Tools/wt.exe" and [p[p.index("--title") + 1] for p in panes] == ["a", "d", "b", "c", "e"]
    assert [p[p.index("--size") + 1] for p in panes if "--size" in p] == ["0.667", "0.500", "0.500"]
    assert sum("-V" in p for p in panes) == 1
    assert ["move-focus", "left"] in groups and ["move-focus", "right"] in groups
