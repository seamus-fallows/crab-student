"""vast: the REST client, against the fake Vast API (a real HTTP server on 127.0.0.1)."""

import os
import time
import urllib.error
import urllib.request

import pytest

from crab_student import core, vast

LABEL = "cs:job1:0123abcd"


def answer_next(fake, count: int, status: int, reply: dict | None = None) -> None:
    """The fake answers the next `count` requests with `status` and `reply`, then as usual."""
    real, left = fake.handle, [count]

    def handle(method, path, query, body):
        if left[0]:
            left[0] -= 1
            return status, reply or {"success": False, "msg": "try again later"}
        return real(method, path, query, body)

    fake.handle = handle


def test_calls_carry_the_key_and_a_refused_key_says_so(fake_vast):
    assert vast.current_user()["email"] == "ada@example.com"
    core.write_private(core.vast_key_file(), "stale-key\n")
    with pytest.raises(vast.VastError) as e:
        vast.current_user()
    assert e.value.status == 401 and "key" in e.value.message.lower()
    assert vast.current_user(key=fake_vast.key)["id"] == 1  # (init checks a new key before saving it)
    assert len(fake_vast.requests) == 3  # (a refused key is not retried)


def test_without_a_key_nothing_is_sent(fake_vast):
    core.vast_key_file().unlink()
    with pytest.raises(core.CrabError):
        vast.instances()
    assert fake_vast.requests == []


def test_the_2fa_session_key_is_sent_while_there_is_one(fake_vast):
    core.write_private(core.vast_key_file(), "long-lived-key\n")
    core.write_private(core.vast_session_file(), fake_vast.key + "\n")
    assert vast.current_user()["id"] == 1
    core.vast_session_file().unlink()
    with pytest.raises(vast.VastError) as e:
        vast.current_user()
    assert e.value.status == 401


def test_a_missing_or_expired_2fa_login_says_to_log_in_and_never_reads_as_gone(fake_vast):
    iid = fake_vast.add_instance(1, LABEL)
    needs_2fa = (
        "Authorization Error. Your key lacks proper privileges to perform this action as it requires you to "
        "have logged in using Two Factor Authentication"
    )
    answer_next(fake_vast, 1, 401, {"success": False, "msg": needs_2fa})
    with pytest.raises(vast.VastError) as e:
        vast.rent(7, {"label": LABEL})
    assert e.value.status == 401 and "vastai tfa login" in e.value.message
    answer_next(fake_vast, 2, 404, {"success": False, "msg": vast.SESSION_EXPIRED})
    with pytest.raises(vast.VastError) as e:
        vast.instance(iid)  # (not None: the machine is still there)
    assert e.value.status == 401
    with pytest.raises(vast.VastError):
        vast.destroy(iid)  # (not "already gone")
    assert list(fake_vast.instances) == [iid]
    assert len(fake_vast.calls("PUT", "/api/v0/asks/")) == 1  # (a refused rent is not retried)


def test_vast_url_overrides_the_address(fake_vast, monkeypatch):
    monkeypatch.setenv("VAST_URL", os.environ["VAST_URL"] + "/")
    assert vast.current_user()["id"] == 1
    monkeypatch.delenv("VAST_URL")
    assert vast.base_url() == "https://console.vast.ai"


def test_calls_are_retried_on_server_errors_but_not_on_refusals(fake_vast):
    answer_next(fake_vast, 2, 503)
    assert vast.current_user()["id"] == 1
    answer_next(fake_vast, 3, 500)
    with pytest.raises(vast.VastError):
        vast.current_user()
    assert len(fake_vast.calls("GET", "/api/v0/users/current")) == 6
    answer_next(fake_vast, 1, 200, {"success": False, "msg": "account suspended"})
    with pytest.raises(vast.VastError, match="account suspended"):
        vast.current_user()
    assert len(fake_vast.calls("GET", "/api/v0/users/current")) == 7


def test_connection_errors_are_retried_except_for_a_rent(fake_vast, monkeypatch):
    real, drops = urllib.request.urlopen, [2]

    def urlopen(request, timeout):
        if drops[0]:
            drops[0] -= 1
            raise urllib.error.URLError(ConnectionResetError("connection reset"))
        return real(request, timeout=timeout)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert vast.current_user()["id"] == 1
    drops[0] = 1
    with pytest.raises(vast.RentUnknown):
        vast.rent(7, {"label": LABEL})
    assert fake_vast.calls("PUT", "/api/v0/asks/") == []  # (tried once, and that try never arrived)


def test_a_rent_returns_the_new_instance_and_a_reply_without_it_is_unknown(fake_vast):
    iid = vast.rent(7, {"label": LABEL, "image": "vastai/base-image:cuda-12.8.1-auto"})
    assert fake_vast.instances[iid]["label"] == LABEL
    answer_next(fake_vast, 1, 200, {"success": True})
    with pytest.raises(vast.RentUnknown):
        vast.rent(7, {"label": LABEL})


@pytest.mark.parametrize(
    ("outcome", "created"), [("error_created", True), ("error_none", False), ("slow_created", True)]
)
def test_a_rent_whose_outcome_is_unknown_is_never_retried(fake_vast, monkeypatch, outcome, created):
    monkeypatch.setattr(vast, "RENT_TIMEOUT", 0.2)
    fake_vast.slow_seconds = 0.5
    fake_vast.rent_outcome[7] = outcome
    with pytest.raises(vast.RentUnknown):
        vast.rent(7, {"label": LABEL})
    assert len(fake_vast.calls("PUT", "/api/v0/asks/")) == 1
    assert [i["label"] for i in fake_vast.instances.values()] == ([LABEL] if created else [])
    if outcome == "slow_created":
        time.sleep(fake_vast.slow_seconds)  # (the fake's late reply fails now, not during another test)


def test_a_garbled_rent_success_is_unknown_but_success_false_is_a_refusal(fake_vast, monkeypatch):
    answer_next(fake_vast, 1, 200, {"success": False, "msg": "offer is no longer available"})
    with pytest.raises(vast.VastError):
        vast.rent(7, {"label": LABEL})
    real_send = vast._send  # (Vast rents, but its reply arrives garbled)
    monkeypatch.setattr(vast, "_send", lambda *a: (real_send(*a)[0], b"<html>Bad Gateway</html>"))
    with pytest.raises(vast.RentUnknown):
        vast.rent(7, {"label": LABEL})
    assert len(fake_vast.calls("PUT", "/api/v0/asks/")) == 2  # (one each: never retried)


@pytest.mark.parametrize(("outcome", "status"), [("taken", 400), ("no_credit", 402)])
def test_a_refused_rent_is_a_vast_error(fake_vast, outcome, status):
    fake_vast.rent_outcome[7] = outcome
    with pytest.raises(vast.VastError) as e:
        vast.rent(7, {"label": LABEL})
    assert e.value.status == status and not fake_vast.instances
    assert len(fake_vast.calls("PUT", "/api/v0/asks/")) == 1


def test_instances_reads_every_page(fake_vast):
    made = [fake_vast.add_instance(n, f"cs:job{n}:0") for n in range(1, 6)]
    assert [i["id"] for i in vast.instances()] == made
    assert len(fake_vast.calls("GET", "/api/v1/instances/")) == 3  # (the fake pages by 2)


def test_an_instance_is_none_once_gone_but_an_odd_reply_is_an_error(fake_vast):
    iid = fake_vast.add_instance(1, LABEL)
    assert vast.instance(iid)["label"] == LABEL
    assert vast.instance(iid + 1) is None
    answer_next(fake_vast, 1, 200, {"success": True})
    with pytest.raises(vast.VastError):
        vast.instance(iid)  # (a glitch must not look like "gone")


def test_stop_start_ssh_key_and_destroy(fake_vast):
    iid = fake_vast.add_instance(1, LABEL)
    vast.set_state(iid, "stopped")
    assert not vast.wants_running(vast.instance(iid))
    vast.set_state(iid, "running")
    assert vast.running(vast.instance(iid))
    vast.add_ssh_key(iid, "ssh-ed25519 AAAA me\n")
    answer_next(fake_vast, 1, 400, {"success": False, "msg": "SSH key already exists on this instance"})
    vast.add_ssh_key(iid, "ssh-ed25519 AAAA me\n")
    assert fake_vast.ssh_keys[iid] == ["ssh-ed25519 AAAA me"]
    vast.destroy(iid)
    vast.destroy(iid)  # already gone: fine
    assert iid not in fake_vast.instances
    with pytest.raises(vast.VastError) as e:
        vast.add_ssh_key(iid, "ssh-ed25519 AAAA me")
    assert e.value.status == 404
    with pytest.raises(vast.VastError):
        vast.set_state(iid, "running")
    assert len(fake_vast.calls("PUT", f"/api/v0/instances/{iid}/")) == 3  # (a 404 is not retried)


def test_the_address_is_the_direct_port_when_there_is_one():
    ports = {"22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40122"}]}
    inst = {"public_ipaddr": "203.0.113.7\n", "ports": ports, "ssh_host": "ssh5.vast.ai", "ssh_port": 12345}
    assert vast.address(inst) == ("203.0.113.7", 40122)
    assert vast.address({**inst, "ports": {}}) == ("ssh5.vast.ai", 12345)
    assert vast.address({**inst, "public_ipaddr": None}) == ("ssh5.vast.ai", 12345)
    assert vast.address({"actual_status": "loading", "ports": None}) is None


@pytest.mark.parametrize(
    ("actual", "intended", "running", "wants"),
    [
        ("running", "running", True, True),
        ("running", "stopped", False, True),  # on its way down: still counts
        ("loading", "running", False, True),
        ("created", None, False, True),
        ("exited", "stopped", False, False),
    ],
)
def test_running_and_wants_running(actual, intended, running, wants):
    inst = {"actual_status": actual, "intended_status": intended}
    assert (vast.running(inst), vast.wants_running(inst)) == (running, wants)
    assert not vast.running(None) and not vast.wants_running(None)
