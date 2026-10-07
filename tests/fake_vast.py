"""A fake Vast REST API: a real HTTP server on 127.0.0.1 with the endpoints crab-student uses.

Tests set its behaviour: `offers` (what searches return, filtered like Vast would unless
`ignore_filters`), `rent_outcome[offer_id]` (ok, taken, no_credit, error_created, error_none,
slow_created), `boot[offer_id]` (polls until running, or "never", "boot_error", "exits", "stale_error":
runs at once, an old error still in its status message; "no_ssh": runs at once, with no ssh address),
`start_polls` (polls a machine started again still reads "exited", with an error, while its host makes
room), `lookup_failures` (machine lookups answered 503 from now on, as when Vast is down), and read
`requests` (every call) and `instances`.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

OPS = {"eq": lambda a, b: a == b, "gte": lambda a, b: a >= b, "gt": lambda a, b: a > b,
       "lte": lambda a, b: a <= b, "lt": lambda a, b: a < b, "in": lambda a, b: a in b}  # fmt: skip
CONTROL = {"type", "order", "limit", "allocated_storage"}
NO_ROOM = "Error: the host's GPUs are in use by another client; waiting for them"


def offer(id: int, **fields) -> dict:
    """A realistic offer (trimmed to the fields crab-student reads)."""
    base = {"id": id, "machine_id": id * 10, "gpu_name": "RTX 4090", "num_gpus": 1, "gpu_ram": 24564,
            "compute_cap": 890, "gpu_mem_bw": 1008.0, "dph_total": 0.40, "reliability2": 0.995, "inet_down": 800.0,
            "inet_up": 600.0, "cuda_max_good": 12.9, "geolocation": "Quebec, CA", "disk_space": 200.0,
            "cpu_cores_effective": 16.0, "cpu_cores": 64, "cpu_name": "AMD EPYC 7B13 64-Core Processor",
            "cpu_ram": 64000, "verification": "verified", "verified": True, "rentable": True, "rented": False,
            "external": False, "direct_port_count": 10}  # fmt: skip
    return {**base, **fields}


def cpu_offer(id: int, **fields) -> dict:
    """A machine with plenty of CPU (Vast has no machine without a GPU: this one's is a cheap one)."""
    base = {"gpu_name": "RTX 3060", "gpu_ram": 12288, "compute_cap": 860, "dph_total": 0.20,
            "cpu_cores_effective": 32.0, "cpu_cores": 32, "cpu_name": "AMD Ryzen 9 7950X 16-Core Processor",
            "cpu_ram": 128000}  # fmt: skip
    return offer(id, **{**base, **fields})


class FakeVast:
    def __init__(self) -> None:
        self.key = "test-key"
        self.offers: list[dict] = []
        self.instances: dict[int, dict] = {}
        self.requests: list[tuple[str, str, dict | None]] = []
        self.rent_outcome: dict[int, str] = {}
        self.boot: dict[int, object] = {}
        self.ignore_filters = False
        self.user = {"id": 1, "email": "ada@example.com", "credit": 25.0}
        self.ssh_keys: dict[int, list[str]] = {}
        self.slow_seconds = 1.0
        self.start_polls = 0
        self.lookup_failures = 0
        self._next = 5000
        self.lock = threading.Lock()

    # ---- helpers for tests ----------------------------------------------------------------------

    def calls(self, method: str, prefix: str) -> list:
        return [r for r in self.requests if r[0] == method and r[1].startswith(prefix)]

    def add_instance(self, offer_id: int, label: str, status: str = "running", **fields) -> int:
        with self.lock:
            self._next += 1
            iid = self._next
        o = next((x for x in self.offers if x["id"] == offer_id), offer(offer_id))
        self.instances[iid] = {"id": iid, "label": label, "offer_id": offer_id, "machine_id": o["machine_id"],
                               "dph_total": o["dph_total"], "actual_status": status, "intended_status": "running",
                               "cur_state": "running", "status_msg": "", "public_ipaddr": "127.0.0.1",
                               "ports": {"22/tcp": [{"HostPort": str(20000 + iid % 1000)}]},
                               "ssh_host": "ssh1.vast.example", "ssh_port": 30000 + iid % 1000, "polls": 0,
                               **fields}  # fmt: skip
        return iid

    # ---- the API --------------------------------------------------------------------------------

    def handle(self, method: str, path: str, query: dict, body: dict | None) -> tuple[int, dict]:
        parts = [p for p in path.split("/") if p]
        if path == "/api/v0/bundles/" and method == "POST":
            return 200, {"offers": self._search(body or {})}
        if len(parts) == 4 and parts[2] == "asks" and method == "PUT":
            return self._rent(int(parts[3]), body or {})
        if path == "/api/v1/instances/" and method == "GET":
            return self._list(query)
        if len(parts) >= 4 and parts[2] == "instances":
            iid = int(parts[3])
            inst = self.instances.get(iid)
            if len(parts) == 5 and parts[4] == "ssh" and method == "POST":
                if inst is None:
                    return 404, {"success": False, "msg": "no such instance"}
                self.ssh_keys.setdefault(iid, []).append(body["ssh_key"])
                return 200, {"success": True}
            if inst is None:
                return 404, {"success": False, "msg": "no such instance"}
            if method == "GET":
                if self.lookup_failures:
                    self.lookup_failures -= 1
                    return 503, {"success": False, "msg": "service unavailable"}
                self._advance(inst)
                return 200, {"instances": {k: v for k, v in inst.items() if k != "polls"}}
            if method == "PUT":
                state = (body or {}).get("state")
                inst["intended_status"] = inst["cur_state"] = state
                inst["actual_status"] = "running" if state == "running" else "exited"
                if state == "running" and self.start_polls:
                    inst.update(actual_status="exited", waits=self.start_polls, status_msg=NO_ROOM)
                return 200, {"success": True}
            if method == "DELETE":
                del self.instances[iid]
                return 200, {"success": True}
        if path == "/api/v0/users/current" and method == "GET":
            return 200, dict(self.user)
        return 404, {"success": False, "msg": f"unknown endpoint {method} {path}"}

    def _search(self, q: dict) -> list[dict]:
        rows = []
        for o in self.offers:
            if "ask_contract_id" in q:  # (offers by id: Vast applies no other filter)
                if all(OPS[op](o["id"], v) for op, v in q["ask_contract_id"].items()):
                    rows.append(o)
                continue
            if self.ignore_filters or all(
                    field in o and all(OPS[op](o[field], v) for op, v in cond.items())
                    for field, cond in q.items() if field not in CONTROL):  # fmt: skip
                rows.append(o)
        return sorted(rows, key=lambda o: o["dph_total"])[: q.get("limit", 1000)]

    def _rent(self, offer_id: int, body: dict) -> tuple[int, dict]:
        outcome = self.rent_outcome.get(offer_id, "ok")
        if outcome == "taken":
            return 400, {"success": False, "error": "no_such_ask", "msg": "offer is no longer available"}
        if outcome == "no_credit":
            return 402, {"success": False, "msg": "insufficient credit"}
        if outcome == "error_none":
            return 500, {"success": False, "msg": "internal error"}
        iid = self.add_instance(offer_id, body.get("label", ""), status="loading", rent_body=body)
        if outcome == "error_created":
            return 500, {"success": False, "msg": "internal error"}
        if outcome == "slow_created":
            time.sleep(self.slow_seconds)
        return 200, {"success": True, "new_contract": iid}

    def _advance(self, inst: dict) -> None:
        inst["polls"] += 1
        if inst.get("waits"):
            inst["waits"] -= 1
            if not inst["waits"]:
                inst.update(actual_status="running", status_msg="")
        if inst["actual_status"] != "loading":
            return
        script = self.boot.get(inst["offer_id"], 1)
        if script in ("boot_error", "stale_error"):
            inst["status_msg"] = "Error response from daemon: failed to set up container networking"
            if script == "stale_error":
                inst["actual_status"] = "running"
        elif script == "exits":
            inst["actual_status"] = "exited"
        elif script == "no_ssh":
            inst.update(actual_status="running", ports={}, ssh_host="")
        elif script != "never" and inst["polls"] >= script:
            inst["actual_status"] = "running"

    def _list(self, query: dict) -> tuple[int, dict]:
        rows = sorted(self.instances.values(), key=lambda i: i["id"])
        start = int(query.get("after_token", "0") or 0)
        page = rows[start : start + 2]
        more = start + 2 < len(rows)
        return 200, {"instances": [{k: v for k, v in r.items() if k != "polls"} for r in page],
                     "next_token": str(start + 2) if more else None}  # fmt: skip


class _Handler(BaseHTTPRequestHandler):
    fake: FakeVast

    def _serve(self) -> None:
        url = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw.strip() else None
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.fake.requests.append((self.command, url.path, body))
        if self.headers.get("Authorization") != f"Bearer {self.fake.key}":
            status, reply = 401, {"success": False, "msg": "invalid user key"}
        else:
            status, reply = self.fake.handle(self.command, url.path, query, body)
        data = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = _serve

    def log_message(self, *args) -> None:
        pass


def serve(fake: FakeVast) -> tuple[ThreadingHTTPServer, str]:
    handler = type("Handler", (_Handler,), {"fake": fake})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()  # (a quick shutdown)
    return server, f"http://127.0.0.1:{server.server_address[1]}"
