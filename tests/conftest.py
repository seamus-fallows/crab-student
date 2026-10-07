"""Shared fixtures. Every test runs with a temporary home (so ~/.crab-student, ~/.ssh, ~/.claude and the
Vast key file are temporary), an in-memory keyring, no network (VAST_URL points at a closed port) and no
real `gh`. `fake_vast`, `fake_github`, `user_repo` and `machines` give the fakes."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import keyring
import keyring.backend
import pytest
from fake_gh import FakeGitHub, git
from fake_vast import FakeVast, serve

from crab_student import agents, core, github, launch, machine, vast


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("VAST_URL", "http://127.0.0.1:9")  # (nothing listens: an unexpected call fails)
    monkeypatch.delenv("CRAB_STUDENT_GITHUB_URL", raising=False)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    keyring.set_keyring(MemoryKeyring())
    monkeypatch.setattr(vast, "DELAYS", (0.01, 0.01))
    monkeypatch.setattr(launch, "POLL", 0.01)
    monkeypatch.setattr(launch, "LABEL_WAIT", 0.3)
    monkeypatch.setattr(agents, "FOLLOW_POLL", 0.01)

    def no_gh(*args):
        raise AssertionError(f"a test ran gh without the fake_github fixture: {args}")

    monkeypatch.setattr(github, "gh", no_gh)
    real_tool = core.tool  # (this computer's VS Code is never started, nor asked for its version)
    monkeypatch.setattr(core, "tool", lambda name: None if name == "code" else real_tool(name))
    return home


@pytest.fixture(autouse=True)
def windows(monkeypatch) -> list[str]:
    """The launch windows crab-student would open (the agent each follows), never opened for real."""
    opened: list[str] = []
    monkeypatch.setattr(machine, "open_launch_window", lambda name: opened.append(name) or True)
    return opened


@pytest.fixture
def fake_vast(monkeypatch) -> FakeVast:
    fake = FakeVast()
    server, url = serve(fake)
    monkeypatch.setenv("VAST_URL", url)
    core.write_private(core.vast_key_file(), fake.key + "\n")
    yield fake
    server.shutdown()
    server.server_close()


@pytest.fixture
def fake_github(tmp_path, monkeypatch) -> FakeGitHub:
    fake = FakeGitHub(tmp_path / "github")
    fake.root.mkdir()
    monkeypatch.setenv("CRAB_STUDENT_GITHUB_URL", str(fake.root))
    monkeypatch.setattr(github, "gh", fake.gh)
    return fake


@pytest.fixture
def user_repo(tmp_path, fake_github) -> Path:
    """The user's clone of ada/proj (its remote looks like GitHub; git is pointed at the bare repo)."""
    fake_github.create_repo("ada/proj")
    path = tmp_path / "proj"
    git(tmp_path, "clone", "-q", str(fake_github.bare("ada/proj")), "proj")
    git(path, "remote", "set-url", "origin", "git@github.com:ada/proj.git")
    git(path, "config", f"url.{fake_github.root.as_posix()}/.insteadOf", "git@github.com:")
    return path


class FakeMachines:
    """Machines as the local side reaches them over ssh: `replies[(name, command)]` (a dict, or an exception
    to raise), `uploads[name]` ({file: (bytes, mode)}), `setup_result` (None for success, or an error)."""

    def __init__(self) -> None:
        self.replies: dict[tuple[str, str], object] = {}
        self.calls: list[tuple[str, list[str], bytes | None]] = []
        self.uploads: dict[str, dict[str, tuple[bytes, int]]] = {}
        self.setup_result: Exception | None = None
        self.reachable = True

    def remote(self, name, args, *, input=None, timeout=60):
        self.calls.append((name, list(args), input))
        reply = self.replies.get((name, args[0]), {})
        if isinstance(reply, Exception):
            raise reply
        return reply

    def upload_and_setup(self, name, data, on_line, timeout=600):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            self.uploads[name] = {m.name: (tar.extractfile(m).read(), m.mode) for m in tar.getmembers()}
        for i, label in enumerate(
            (
                "environment",
                "repository",
                "dependencies",
                "Claude Code",
                "agent settings",
                "starting the agent",
            ),
            1,
        ):
            on_line(f"step {i}/6 {label}")
        if self.setup_result:
            raise self.setup_result
        return []


@pytest.fixture
def machines(monkeypatch) -> FakeMachines:
    fake = FakeMachines()
    monkeypatch.setattr(machine, "remote", fake.remote)
    monkeypatch.setattr(machine, "upload_and_setup", fake.upload_and_setup)
    monkeypatch.setattr(machine, "reachable", lambda name: fake.reachable)
    monkeypatch.setattr(machine, "forget_host_key", lambda name: None)
    return fake


@pytest.fixture
def ready(isolated) -> None:
    """What init leaves behind: the SSH key, GitHub's host key and a Claude token."""
    ssh = core.ssh_dir()
    ssh.mkdir(parents=True, exist_ok=True)
    (ssh / "id_ed25519").write_text("private\n")
    (ssh / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test\n")
    github.known_hosts_file().write_text(github.HOST_KEY + "\n")
    core.set_secret(core.TOKEN, "sk-ant-oat01-" + "x" * 80)
