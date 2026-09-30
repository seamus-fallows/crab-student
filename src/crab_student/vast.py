"""The Vast.ai REST client (urllib, the user's key from the vastai CLI's key file)."""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from crab_student import core

DELAYS = (2.0, 5.0)  # between retries of a call that may be repeated (tests shorten them)
RENT_TIMEOUT = 60.0
RETRY_STATUS = (429, 500, 502, 503, 504)


class VastError(core.CrabError):
    """Vast refused a request, or could not be reached after retries."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RentUnknown(Exception):
    """A rent request whose outcome is unknown: it may have rented a machine."""


def base_url() -> str:
    return os.environ.get("VAST_URL", "https://console.vast.ai").rstrip("/")


def api_key() -> str:
    try:
        key = core.vast_key_file().read_text(encoding="utf-8").strip()
    except OSError:
        key = ""
    if not key:
        raise core.CrabError("crab-student is not set up yet (no Vast key). Run: crab-student init")
    return key


def _send(
    method: str,
    path: str,
    body: dict | None,
    timeout: float,
    query: dict | None = None,
    key: str | None = None,
) -> tuple[int, bytes]:
    url = base_url() + path
    if query:
        url += "?" + urllib.parse.urlencode(
            {k: v if isinstance(v, str) else json.dumps(v) for k, v in query.items()}
        )
    headers = {
        "Authorization": f"Bearer {key or api_key()}",
        "Accept": "application/json",
        "User-Agent": "crab-student",
    }
    data = None
    if method != "GET":
        data, headers["Content-Type"] = json.dumps(body or {}).encode(), "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read()


def _reason(raw: bytes, status: int) -> str:
    text = raw.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and (data.get("msg") or data.get("error") or data.get("message")):
        return str(data.get("msg") or data.get("error") or data.get("message"))
    return text[:200] or f"HTTP {status}"


def _parse(status: int, raw: bytes, what: str) -> dict:
    if status in (401, 403):
        raise VastError(
            f"Vast refused the API key while trying to {what} (HTTP {status}). Check the key in "
            f"{core.vast_key_file()}, or run crab-student init.",
            status,
        )
    if not 200 <= status < 300:
        raise VastError(f"Vast could not {what}: {_reason(raw, status)}", status)
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise VastError(f"Vast sent an unexpected reply while trying to {what}.", status) from None
    if isinstance(data, dict) and data.get("success") is False:
        raise VastError(f"Vast could not {what}: {_reason(raw, status)}", status)
    return data if isinstance(data, dict) else {"data": data}


def request(
    method: str,
    path: str,
    what: str,
    body: dict | None = None,
    *,
    query: dict | None = None,
    timeout: float = 30,
    key: str | None = None,
) -> dict:
    """One call, retried on connection problems and server errors (never use it to rent)."""
    problem = ""
    for delay in (*DELAYS, None):
        try:
            status, raw = _send(method, path, body, timeout, query, key)
            if status not in RETRY_STATUS:
                return _parse(status, raw, what)
            problem = f"HTTP {status}: {_reason(raw, status)}"
        except (OSError, http.client.HTTPException) as e:  # URLError and timeouts are OSErrors
            problem = str(getattr(e, "reason", "") or e) or type(e).__name__
        if delay is not None:
            time.sleep(delay)
    raise VastError(f"Vast could not {what}: {problem}. Try again in a few minutes.")


def search(query: dict) -> list[dict]:
    data = request("POST", "/api/v0/bundles/", "search offers", query)
    return [o for o in data.get("offers") or [] if isinstance(o, dict)]


def rent(offer_id: int, body: dict) -> int:
    """Rent an offer: the new instance id. Never retried: a timeout or server error may still have rented
    (RentUnknown); a definite refusal raises VastError."""
    try:
        status, raw = _send("PUT", f"/api/v0/asks/{int(offer_id)}/", body, RENT_TIMEOUT)
    except (OSError, http.client.HTTPException) as e:
        raise RentUnknown(str(e) or type(e).__name__) from None
    if status >= 500:
        raise RentUnknown(f"HTTP {status}")
    if 200 <= status < 300:
        try:
            json.loads(raw)
        except ValueError:  # (a garbled success may well have rented)
            raise RentUnknown(f"an unexpected reply: {raw[:200].decode('utf-8', 'replace')}") from None
    data = _parse(status, raw, f"rent offer {offer_id}")
    iid = data.get("new_contract")
    if not isinstance(iid, int) or iid <= 0:
        raise RentUnknown(
            f"Vast did not say which machine it rented ({raw[:200].decode('utf-8', 'replace')})"
        )
    return iid


def instances(key: str | None = None) -> list[dict]:
    """Every instance on the account, all pages."""
    query: dict = {"select_filters": {}, "order_by": [{"col": "id", "dir": "asc"}], "limit": 25}
    rows: list[dict] = []
    while True:
        data = request("GET", "/api/v1/instances/", "list your machines", query=query, key=key)
        rows += [r for r in data.get("instances") or [] if isinstance(r, dict)]
        token = data.get("next_token")
        if not token or token == query.get("after_token"):
            return rows
        query["after_token"] = token


def instance(instance_id: int) -> dict | None:
    """One instance, or None once it no longer exists."""
    try:
        data = request(
            "GET",
            f"/api/v0/instances/{int(instance_id)}/",
            f"look up machine {instance_id}",
            query={"owner": "me"},
        )
    except VastError as e:
        if e.status == 404:
            return None
        raise
    row = data.get("instances")
    if "instances" not in data:  # (a glitch must not look like "gone")
        raise VastError(f"Vast sent an unexpected reply while looking up machine {instance_id}.")
    return row if isinstance(row, dict) and row.get("id") is not None else None


def set_state(instance_id: int, state: str) -> None:
    """Start ("running") or stop ("stopped") an instance."""
    verb = "start" if state == "running" else "stop"
    request(
        "PUT", f"/api/v0/instances/{int(instance_id)}/", f"{verb} machine {instance_id}", {"state": state}
    )


def destroy(instance_id: int) -> None:
    """Destroy an instance (irreversible). An instance that is already gone is fine."""
    try:
        request("DELETE", f"/api/v0/instances/{int(instance_id)}/", f"destroy machine {instance_id}", {})
    except VastError as e:
        if e.status != 404:
            raise


def add_ssh_key(instance_id: int, public_key: str) -> None:
    try:
        request(
            "POST",
            f"/api/v0/instances/{int(instance_id)}/ssh/",
            f"add the SSH key to machine {instance_id}",
            {"ssh_key": public_key.strip()},
        )
    except VastError as e:
        if "already" not in e.message.lower():
            raise


def current_user(key: str | None = None) -> dict:
    return request("GET", "/api/v0/users/current", "look up your account", key=key)


# ---- instance fields -----------------------------------------------------------------------------


def address(inst: dict) -> tuple[str, int] | None:
    """(host, port) for SSH: the machine's direct port when it has one, else Vast's SSH proxy."""
    ip = str(inst.get("public_ipaddr") or "").strip()
    ports = inst.get("ports") if isinstance(inst.get("ports"), dict) else {}
    for mapping in ports.get("22/tcp") or []:
        port = mapping.get("HostPort") if isinstance(mapping, dict) else None
        if ip and str(port or "").isdigit():
            return ip, int(port)
    host, port = str(inst.get("ssh_host") or "").strip(), inst.get("ssh_port")
    return (host, int(port)) if host and str(port or "").isdigit() else None


def running(inst: dict | None) -> bool:
    return bool(inst) and inst.get("actual_status") == "running" and inst.get("intended_status") != "stopped"


def wants_running(inst: dict | None) -> bool:
    """Running, or on its way up (it counts against max_running_machines)."""
    if not inst:
        return False
    intended = inst.get("intended_status") or inst.get("cur_state")
    return intended == "running" or inst.get("actual_status") in ("running", "loading", "created")
