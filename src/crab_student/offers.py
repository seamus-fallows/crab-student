"""Requirements, the offer search, the picking rule and the budget."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field

from crab_student import core, vast

REGIONS = {  # continent -> ISO country codes; `regions` entries are these names or 2-letter codes
    "north-america": {"US", "CA", "MX"},
    "south-america": {"AR", "BR", "CL", "CO", "PE", "UY"},
    "europe": set(
        "AT BE BG CH CY CZ DE DK EE ES FI FR GB GR HR HU IE IS IT LT LU LV MT NL NO PL PT RO RS SE SI SK UA".split()
    ),
    "asia": set("AE HK ID IL IN JP KR MY PH SG TH TR TW VN".split()),
    "oceania": {"AU", "NZ"},
    "africa": {"EG", "KE", "MA", "NG", "ZA"},
}


@dataclass
class Req:
    """What a task needs. gpus 0 is a CPU task (cheap GPU hosts may still be picked for it)."""

    hours: float
    gpus: int = 0
    gpu_ram_gb: float = 0
    gpu_models: list[str] = field(default_factory=list)
    cpus: int = 4
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
    budget_usd: float
    auto_budget: bool
    considered: int

    def offers(self) -> list[dict]:
        return [self.primary, *self.fallbacks]


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


def allowed_countries(regions: list[str]) -> set[str]:
    codes: set[str] = set()
    for r in regions:
        codes |= REGIONS.get(r.lower(), {r.upper()})
    return codes


def fits(o: dict, req: Req, cfg: dict, now: float) -> bool:
    """The client-side recheck of every requirement (Vast's filter is not always exact)."""
    price, rel = _num(o, "dph_total"), _num(o, "reliability2")
    rel = _num(o, "reliability") if rel is None else rel
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
    end = _num(o, "end_date")
    if ok and end and end < now + 3600 * (cfg["budget_factor"] * req.hours + 1):
        ok = False
    if ok and cfg["regions"]:
        ok = country(o) in allowed_countries(cfg["regions"])
    return ok


def rank(offers: list[dict], gpu: bool) -> list[dict]:
    """The deterministic order: for GPU work, drop offers clearly worse in speed per dollar; among offers
    within 10% of the lowest price left, the fastest download first; then by price and id."""

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
        (o for o in pool if o["dph_total"] <= 1.10 * floor),
        key=lambda o: (-(_num(o, "inet_down") or 0), o["dph_total"], o["id"]),
    )
    rest = sorted(
        (o for o in offers if o not in similar),
        key=lambda o: (o["dph_total"], -(_num(o, "inet_down") or 0), o["id"]),
    )
    return similar + rest


def fallbacks(ranked: list[dict]) -> list[dict]:
    """Up to 2 backups after the pick: other machines (a boot failure is often the host's fault) at most
    1.25x its price."""
    primary, picked = ranked[0], []
    seen = {primary.get("machine_id") or primary["id"]}
    for o in ranked[1:]:
        machine = o.get("machine_id") or o["id"]
        if len(picked) < 2 and machine not in seen and o["dph_total"] <= 1.25 * primary["dph_total"]:
            picked.append(o)
            seen.add(machine)
    return picked


def search(req: Req, cfg: dict) -> list[dict]:
    now = time.time()
    return [o for o in vast.search(query(req, cfg)) if fits(o, req, cfg, now)]


def plan(req: Req, cfg: dict, budget: float | None = None) -> Plan:
    """The machine to rent, its backups and the budget: CrabError(4) when nothing fits the hardware,
    CrabError(3) when an explicit budget is too small for anything that does."""
    found = search(req, cfg)
    if not found:
        where = f" in {', '.join(cfg['regions'])}" if cfg["regions"] else ""
        more = ', or allow more regions: crab-student config regions ""' if cfg["regions"] else ""
        raise core.CrabError(
            f"no machine fits: no offer has {req.describe()}{where} right now. See what is "
            f"available with crab-student offers, loosen the requirements{more}.",
            4,
        )
    pool = found
    if budget is not None:
        pool = [o for o in found if o["dph_total"] * req.hours <= budget]
        if not pool:
            low = min(o["dph_total"] for o in found)
            raise core.CrabError(
                f"a budget of {core.usd(budget)} is too small: the cheapest machine that fits costs "
                f"${low:.2f}/hr, so {req.hours:g} h needs at least "
                f"{core.usd(core.ceil_cents(low * req.hours))}.",
                3,
            )
    ranked = rank(pool, req.gpu)
    auto = budget is None
    if budget is None:
        hours = max(cfg["budget_factor"] * req.hours, cfg["min_budget_hours"])
        budget = core.ceil_cents(ranked[0]["dph_total"] * hours)
    return Plan(req, ranked[0], fallbacks(ranked), budget, auto, len(found))


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
    """The fields of an offer a worker record keeps."""
    return {
        "id": o["id"],
        "machine_id": o.get("machine_id"),
        "dph_total": o["dph_total"],
        "gpu_host": (_num(o, "num_gpus") or 0) > 0,
        "summary": summary(o),
        "short": short(o),
    }


def req_dict(req: Req) -> dict:
    return asdict(req)
