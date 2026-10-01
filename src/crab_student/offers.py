"""Requirements, the offer search and the picking rule."""

from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass, field

from crab_student import core, vast

# A host's lease must outlast twice the expected hours, and at least a day (plus an hour of margin).
LEASE_FACTOR, MIN_LEASE_HOURS = 2.0, 24.0
SIMILAR_PRICE = 1.20  # offers up to this factor over the cheapest count as the same price
# Backups after the pick: two race at a time, so four leave room for two more rounds after the first pair.
MAX_BACKUPS = 4
REGIONS = {  # continent -> ISO country codes; `regions` entries are these names or 2-letter codes
    "north-america": {"US", "CA", "MX"},
    "south-america": {"AR", "BR", "CL", "CO", "PE", "UY"},
    "europe": set(
        "AT BE BG CH CY CZ DE DK EE ES FI FR GB GR HR HU IE IS IT LT LU LV MT NL NO PL PT RO RS SE SI SK UA".split()
    ),
    "asia": set("AE ID IL IN JP KR MY PH SG TH TR TW VN".split()),
    "oceania": {"AU", "NZ"},
    "africa": {"EG", "KE", "MA", "NG", "ZA"},
}
# Countries where Anthropic's API is unavailable, so Claude Code on a host there cannot work: those Vast
# could list that are missing from https://www.anthropic.com/supported-countries (checked October 2026).
BLOCKED = {"AF", "BY", "CN", "CU", "HK", "IR", "KP", "MM", "MO", "RU", "SY", "VE", "YE"}


@dataclass
class Req:
    """What a task needs. gpus 0 is a CPU task (cheap GPU hosts may still be picked for it). hours, when
    known, only sets how long the host must stay available."""

    hours: float | None = None
    gpus: int = 0
    gpu_ram_gb: float = 0
    gpu_models: list[str] = field(default_factory=list)
    cpus: int = 8
    ram_gb: float = 16
    disk_gb: float = 50
    min_down_mbps: float = 200

    @property
    def gpu(self) -> bool:
        return self.gpus > 0

    def describe(self) -> str:
        parts = []
        if self.gpu:
            gpus = f"{self.gpus} GPU{'s' if self.gpus > 1 else ''}"
            if self.gpu_ram_gb:
                gpus += f" with {self.gpu_ram_gb:g} GB{' each' if self.gpus > 1 else ''}"
            if self.gpu_models:
                gpus += f" ({' or '.join(self.gpu_models)})"
            parts.append(gpus)
        parts += [f"{self.cpus} cores", f"{self.ram_gb:g} GB RAM", f"{self.disk_gb:g} GB disk"]
        return ", ".join(parts)


@dataclass
class Plan:
    req: Req
    primary: dict
    fallbacks: list[dict]
    ranked: list[dict]
    over_cap: bool  # nothing that fits costs less than the cap: these offers need the user's approval

    def offers(self) -> list[dict]:
        return [self.primary, *self.fallbacks]

    def top_price(self) -> float:
        return max(o["dph_total"] for o in self.offers())


def lease_hours(hours: float | None) -> float:
    """How long a host must still be available for a task of `hours` (None: not known)."""
    return max(LEASE_FACTOR * (hours or 0), MIN_LEASE_HOURS) + 1


def image_cuda(image: str) -> float | None:
    """The CUDA version in an image tag ("...cuda-12.8.1..." -> 12.8)."""
    m = re.search(r"cuda-(\d+)\.(\d+)", image)
    return float(f"{m.group(1)}.{m.group(2)}") if m else None


def query(req: Req, cfg: dict) -> dict:
    """Vast's server-side filter (it counts 1 GB as 1000 MB)."""
    q: dict = {
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "verified": {"eq": True},
        "external": {"eq": False},
        "reliability2": {"gte": cfg["min_reliability"]},
        "cpu_cores_effective": {"gte": req.cpus},
        "cpu_ram": {"gte": req.ram_gb * 1000},
        "disk_space": {"gte": req.disk_gb},
        "inet_down": {"gte": req.min_down_mbps},
        "direct_port_count": {"gte": 1},
        "type": "on-demand",
        "order": [["dph_total", "asc"]],
        "limit": 1000,
        "allocated_storage": req.disk_gb,
    }
    if req.gpu:
        q["num_gpus"] = {"eq": req.gpus}
        q["gpu_ram"] = {"gte": req.gpu_ram_gb * 1000}
        if cuda := image_cuda(cfg["image"]):
            q["cuda_max_good"] = {"gte": cuda}
    return q


def _num(o: dict, key: str) -> float | None:
    v = o.get(key)
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def country(o: dict) -> str | None:
    """ "California, US" -> "US"."""
    code = str(o.get("geolocation") or "").rsplit(",", 1)[-1].strip().upper()
    return code if re.fullmatch(r"[A-Z]{2}", code) else None


def continent(code: str | None) -> str | None:
    return next((name for name, codes in REGIONS.items() if code in codes), None)


def allowed_countries(regions: list[str]) -> set[str]:
    codes: set[str] = set()
    for r in regions:
        codes |= REGIONS.get(r.lower(), {r.upper()})
    return codes


def reliability(o: dict) -> float | None:
    rel = _num(o, "reliability2")
    return _num(o, "reliability") if rel is None else rel


def fits(o: dict, req: Req, cfg: dict, now: float) -> bool:
    """The client-side recheck of every requirement (Vast's filter is not always exact)."""
    price, rel = _num(o, "dph_total"), reliability(o)
    ok = (
        o.get("rentable") is not False
        and o.get("rented") is not True
        and o.get("external") is not True
        and o.get("verification", "verified") == "verified"
        and bool(price and price > 0)
        and rel is not None
        and rel >= cfg["min_reliability"]
        and (_num(o, "cpu_cores_effective") or 0) >= req.cpus
        and (_num(o, "cpu_ram") or 0) >= req.ram_gb * 1000
        and (_num(o, "disk_space") or 0) >= req.disk_gb
        and (_num(o, "inet_down") or 0) >= req.min_down_mbps
        and (_num(o, "direct_port_count") or 0) >= 1
    )
    if ok and req.gpu:
        cuda = image_cuda(cfg["image"])
        name = str(o.get("gpu_name") or "").lower()
        ok = (
            _num(o, "num_gpus") == req.gpus
            and (_num(o, "gpu_ram") or 0) >= req.gpu_ram_gb * 1000
            and (cuda is None or (_num(o, "cuda_max_good") or 0) >= cuda)
            and (not req.gpu_models or any(m.lower() in name for m in req.gpu_models))
        )
    if ok and country(o) in BLOCKED:  # (an unknown country stays allowed)
        ok = False
    end = _num(o, "end_date")
    if ok and end and end < now + 3600 * lease_hours(req.hours):
        ok = False
    if ok and cfg["regions"]:
        ok = country(o) in allowed_countries(cfg["regions"])
    return ok


def standing(o: dict) -> float:
    """Reliability and download speed together: a point of reliability (in percent) counts as much as twice
    the download speed."""
    return 100 * (reliability(o) or 0) + math.log2(max(_num(o, "inet_down") or 0, 1))


def distance(o: dict, home: str) -> int:
    """0 in the `home` country, 1 on its continent, 2 farther or unknown."""
    where = country(o)
    if where == home:
        return 0
    near = continent(where) if where else None
    return 1 if near and near == continent(home) else 2


def rank(offers: list[dict], gpu: bool, home: str = "") -> list[dict]:
    """The deterministic order: for GPU work, drop offers clearly worse in speed per dollar; among offers
    within 20% of the lowest price left, the nearest to `home` first (when given), then the best standing
    (reliability and download speed); then by price and id."""

    def value(o: dict) -> float:
        return (_num(o, "dlperf") or 0) / o["dph_total"]

    pool = offers
    if gpu:
        cheapest = min(o["dph_total"] for o in offers)
        band = [o for o in offers if o["dph_total"] <= 1.5 * cheapest]
        best = max(value(o) for o in band)
        if best > 0:
            pool = [o for o in band if value(o) >= best / 1.2]
    floor = min(o["dph_total"] for o in pool)
    similar = sorted(
        (o for o in pool if o["dph_total"] <= SIMILAR_PRICE * floor),
        key=lambda o: (distance(o, home) if home else 0, -standing(o), o["dph_total"], o["id"]),
    )
    rest = sorted(
        (o for o in offers if o not in similar),
        key=lambda o: (o["dph_total"], -(_num(o, "inet_down") or 0), o["id"]),
    )
    return similar + rest


def fallbacks(ranked: list[dict], over_cap: bool = False) -> list[dict]:
    """Up to MAX_BACKUPS backups after the pick, on other machines (a boot failure is often the host's fault). Any
    price up to the approval price will do; over it (the user approves the prices shown), at most 1.25x the pick."""
    primary, picked = ranked[0], []
    seen = {primary.get("machine_id") or primary["id"]}
    for o in ranked[1:]:
        machine = o.get("machine_id") or o["id"]
        if (
            len(picked) < MAX_BACKUPS
            and machine not in seen
            and (not over_cap or o["dph_total"] <= 1.25 * primary["dph_total"])
        ):
            picked.append(o)
            seen.add(machine)
    return picked


def search(req: Req, cfg: dict) -> list[dict]:
    now = time.time()
    return [o for o in vast.search(query(req, cfg)) if fits(o, req, cfg, now)]


def plan(req: Req, cfg: dict, cap: float | None = None, mode: str = "") -> Plan:
    """The machine to rent and its backups, from the offers up to `cap` $/hr (over_cap, and all offers, when
    none is that cheap), near the `home` setting for an interactive agent (latency matters only when the user
    types into it): CrabError(4) when nothing fits the hardware."""
    found = search(req, cfg)
    if not found:
        where = f" in {', '.join(cfg['regions'])}" if cfg["regions"] else ""
        more = ', or allow more regions: crab-student config regions ""' if cfg["regions"] else ""
        raise core.CrabError(
            f"no machine fits: no offer has {req.describe()}{where} right now. See what is "
            f"available with crab-student offers, loosen the requirements{more}.",
            4,
        )
    within = [o for o in found if cap is None or o["dph_total"] <= cap]
    home = cfg["home"].upper() if mode == "interactive" else ""
    ranked = rank(within or found, req.gpu, home)
    return Plan(req, ranked[0], fallbacks(ranked, over_cap=not within), ranked, over_cap=not within)


def summary(o: dict) -> str:
    """ "1x RTX 4090 24 GB, 16 cores, 64 GB RAM, Quebec, CA" (a CPU host leaves out the GPU)."""
    parts = []
    if (_num(o, "num_gpus") or 0) > 0:
        parts.append(
            f"{int(o['num_gpus'])}x {o.get('gpu_name') or 'GPU'} {(_num(o, 'gpu_ram') or 0) / 1024:.0f} GB"
        )
    parts.append(f"{int(_num(o, 'cpu_cores_effective') or 0)} cores")
    parts.append(f"{(_num(o, 'cpu_ram') or 0) / 1000:.0f} GB RAM")
    if o.get("geolocation"):
        parts.append(str(o["geolocation"]))
    return core.ascii_text(", ".join(parts))


def short(o: dict) -> str:
    """ "1x RTX 4090 (Quebec, CA)" or "16 cores (Quebec, CA)"."""
    kind = (
        f"{int(o['num_gpus'])}x {o.get('gpu_name') or 'GPU'}"
        if (_num(o, "num_gpus") or 0) > 0
        else f"{int(_num(o, 'cpu_cores_effective') or 0)} cores"
    )
    return core.ascii_text(f"{kind} ({o['geolocation']})" if o.get("geolocation") else kind)


def kept(o: dict) -> dict:
    """The fields of an offer an agent record keeps."""
    return {
        "id": o["id"],
        "machine_id": o.get("machine_id"),
        "dph_total": o["dph_total"],
        "storage_cost": _num(o, "storage_cost"),  # $/GB/month, also while stopped
        "gpu_host": (_num(o, "num_gpus") or 0) > 0,
        "summary": summary(o),
        "short": short(o),
    }


def req_dict(req: Req) -> dict:
    return asdict(req)
