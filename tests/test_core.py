"""core: settings, the name rule, records, locks, secrets, subprocesses and formatting."""

import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import keyring
import keyring.backends.fail
import keyring.backends.null
import pytest

from crab_student import core

PY = sys.executable


def error_code(fn, *args) -> int:
    with pytest.raises(core.CrabError) as e:
        fn(*args)
    return e.value.code


def test_a_value_is_parsed_by_the_type_of_its_default():
    assert core.parse_value("auto_approve_usd", "12.5") == 12.5
    assert core.parse_value("max_running_machines", " 3 ") == 3
    assert core.parse_value("regions", "CA, europe") == ["CA", "europe"]
    assert core.parse_value("regions", "") == []
    assert core.parse_value("image", " me/image:1 ") == "me/image:1" and core.parse_value("model", "") == ""


@pytest.mark.parametrize(
    ("key", "text"),
    [
        ("max_running_machines", "2.5"),
        ("auto_approve_usd", "lots"),
        ("budget_factor", "nan"),
        ("min_reliability", "1.5"),
        ("disk_gb", "5"),
        ("image", "  "),
        ("regions", "CA, mars"),
        ("regions", "USA"),
        ("workspace_repo", "my repo"),
        ("notify_url", "ftp://example.com"),
        ("colour", "blue"),
    ],
)
def test_an_invalid_value_or_an_unknown_setting_is_a_usage_error(key, text):
    assert error_code(core.parse_value, key, text) == 2
    assert error_code(core.set_config, key, text) == 2
    assert not (core.home() / "config.json").exists()


def test_config_json_keeps_only_the_settings_that_differ_from_the_defaults():
    config = core.home() / "config.json"
    assert core.set_config("idle_minutes", "30") == 30
    assert core.set_config("regions", "CA, europe") == ["CA", "europe"]
    assert json.loads(config.read_text()) == {"idle_minutes": 30, "regions": ["CA", "europe"]}
    assert core.set_config("idle_minutes", "60") == 60  # the default again
    assert core.set_config("regions", None) == []  # reset
    assert json.loads(config.read_text()) == {}
    assert core.load_config() == core.DEFAULTS
    assert error_code(core.set_config, "colour", None) == 2


def test_load_config_ignores_unknown_keys_and_hands_out_copies():
    core.write_json(core.home() / "config.json", {"idle_minutes": 5, "retired_setting": True})
    cfg = core.load_config()
    assert cfg == {**core.DEFAULTS, "idle_minutes": 5}
    cfg["regions"].append("US")
    assert core.load_config()["regions"] == []


def test_the_vast_key_is_where_the_vastai_cli_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert core.vast_key_file() == tmp_path / "xdg" / "vastai" / "vast_api_key"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert core.vast_key_file() == Path.home() / ".config" / "vastai" / "vast_api_key"


def test_the_name_rule():
    for name in ("abc", "job-1", "0-9", "a" * 40):
        core.check_name(name)
    for name in ("ab", "a" * 41, "-abc", "abc-", "Job1", "job_1", "job 1", "abc\n", "../x"):
        assert error_code(core.check_name, name) == 2, name


def test_records_are_listed_by_creation_and_removed_with_their_files():
    assert core.all_records() == []
    core.save({"name": "late", "created": "2026-09-30T12:00:00Z"})
    core.save({"name": "early", "created": "2026-09-30T08:00:00Z", "phase": "ready"})
    (core.workers_dir() / "junk.json").write_text("{half a record")
    assert core.load("early")["phase"] == "ready"
    assert [r["name"] for r in core.all_records()] == ["early", "late"]
    for suffix in (".brief.md", ".key", ".key.pub", ".lock"):
        (core.workers_dir() / f"late{suffix}").write_text("x")
    core.remove("late")
    assert sorted(p.name for p in core.workers_dir().iterdir()) == ["early.json", "junk.json"]
    assert error_code(core.load, "late") == 1


def test_a_save_is_atomic(monkeypatch):
    core.save({"name": "job1", "phase": "ready"})
    with pytest.raises(TypeError):
        core.save({"name": "job1", "phase": object()})  # fails halfway through writing
    assert core.load("job1") == {"name": "job1", "phase": "ready"}
    real, refusals = os.replace, [2]

    def replace(src, dst):  # Windows refuses to replace a file another process has open, for a moment
        if refusals[0]:
            refusals[0] -= 1
            raise PermissionError(13, "in use")
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    core.save({"name": "job1", "phase": "setup"})
    assert core.load("job1")["phase"] == "setup"


HOLDER = """
import sys
from crab_student import core
with core.locked(sys.argv[1]):
    print("held", flush=True)
    sys.stdin.read()
"""


def test_a_lock_held_by_another_process_is_refused_until_that_process_ends():
    cmd = [PY, "-c", HOLDER, "job1"]
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as holder:
        assert holder.stdout.readline().strip() == "held"
        assert core.is_locked("job1") and not core.is_locked("job2")
        with pytest.raises(core.CrabError) as e, core.locked("job1"):
            pass
        assert e.value.code == 1
        threading.Timer(0.3, holder.kill).start()  # (an OS lock dies with its process)
        with core.locked("job1", wait=10):
            pass
    assert not core.is_locked("job1")


def test_secrets_are_kept_in_the_keyring():
    assert core.set_secret(core.TOKEN, "sk-ant-oat01-abc") == "keyring"
    assert core.get_secret(core.TOKEN) == "sk-ant-oat01-abc"
    assert core.get_secret(core.secret_key("HF_TOKEN")) is None
    core.delete_secret(core.TOKEN)
    assert core.get_secret(core.TOKEN) is None
    assert not (core.home() / "secrets").exists()


@pytest.mark.parametrize("broken", [keyring.backends.fail.Keyring, keyring.backends.null.Keyring])
def test_without_a_working_keyring_secrets_go_to_a_private_file(broken):
    working = keyring.get_keyring()
    keyring.set_keyring(broken())  # (fail raises NoKeyringError; null silently keeps nothing)
    assert core.set_secret(core.TOKEN, "old") == "file"
    assert core.get_secret(core.TOKEN) == "old"
    (file,) = (core.home() / "secrets").iterdir()
    if os.name != "nt":
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    core.delete_secret(core.TOKEN)
    assert core.get_secret(core.TOKEN) is None and not file.exists()
    core.set_secret(core.TOKEN, "old")
    keyring.set_keyring(working)  # a keyring that works again takes over from the file
    assert core.get_secret(core.TOKEN) == "old"
    assert core.set_secret(core.TOKEN, "new") == "keyring"
    assert core.get_secret(core.TOKEN) == "new" and not file.exists()


def test_secret_names():
    assert core.secret_names() == []
    core.set_secret_names(["WANDB_API_KEY", "HF_TOKEN", "HF_TOKEN"])
    assert core.secret_names() == ["HF_TOKEN", "WANDB_API_KEY"]
    assert all(core.SECRET_NAME_RE.fullmatch(n) for n in ("HF_TOKEN", "A", "A" * 64))
    assert not any(core.SECRET_NAME_RE.fullmatch(n) for n in ("hf_token", "1ABC", "HF-TOKEN", "A" * 65))


def test_run_reports_the_last_line_a_failed_program_printed():
    script = "import sys; print('progress'); sys.stderr.write('warning: minor\\nfatal: the reason\\n\\n'); sys.exit(3)"
    with pytest.raises(core.CrabError, match="the reason") as e:
        core.run([PY, "-c", script])
    assert "minor" not in e.value.message
    assert core.run([PY, "-c", script], check=False).returncode == 3
    with pytest.raises(core.CrabError, match="only on stdout"):
        core.run([PY, "-c", "print('only on stdout'); raise SystemExit(1)"])
    with pytest.raises(core.CrabError, match="in bytes"):
        core.run([PY, "-c", "import sys; sys.stderr.write('in bytes'); sys.exit(1)"], input=b"")


def test_run_explains_a_missing_program_and_a_timeout_and_passes_bytes_through():
    with pytest.raises(core.CrabError, match="no-such-program-xyz"):
        core.run(["no-such-program-xyz", "--version"])
    with pytest.raises(core.CrabError):
        core.run([PY, "-c", "import time; time.sleep(10)"], timeout=0.5)
    reverse = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])"
    assert core.run([PY, "-c", reverse], input=b"\x00\xffab").stdout == b"ba\xff\x00"


def test_money_and_hours():
    assert core.ceil_cents(2.521) == 2.53
    assert core.ceil_cents(0.07) == 0.07 and core.ceil_cents(1.1 * 3) == 3.3  # (float noise costs no cent)
    assert core.usd(1.5) == "$1.50" and core.usd(None) == "-"
    assert core.fmt_hours(1.2) == "1h12m" and core.fmt_hours(0.9999) == "1h00m"
    assert core.fmt_hours(-0.1) == "0h00m" and core.fmt_hours(None) == "-"


@pytest.mark.skipif(os.name != "nt", reason="Windows' own OpenSSH")
def test_ssh_is_windows_own_openssh_on_windows(tmp_path, monkeypatch):
    own = tmp_path / "System32" / "OpenSSH" / "ssh.exe"
    own.parent.mkdir(parents=True)
    own.touch()
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path))
    monkeypatch.setattr(core.shutil, "which", lambda name: f"C:\\Git\\usr\\bin\\{name}.exe")
    assert core.tool("ssh") == str(own)
    assert core.tool("ssh-keygen") == "C:\\Git\\usr\\bin\\ssh-keygen.exe"  # (not in System32 here)
    assert core.tool("git") == "C:\\Git\\usr\\bin\\git.exe"
