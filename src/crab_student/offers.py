"""The offer search: the fixed rules every offer passes, the filters `offers` browses with, and the offers a launch
names. Which machine to rent is the launcher's choice (data/launch.md)."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from crab_student import core, vast

# A host's lease must outlast twice a long job's hours, and at least a day (plus an hour of margin).
LEASE_FACTOR, MIN_LEASE_HOURS = 2.0, 24.0
CPU_TASK_CPUS, CPU_TASK_RAM_GB = 24, 64  # the least a machine for a task that uses no GPU has
MAX_OFFERS = 5  # a launch's pick and up to four backups
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
class Filters:
    """What `offers` shows. gpus is the range of GPU counts; (0, 0) is a task that uses no GPU, which may run on
    any machine with at least CPU_TASK_CPUS and CPU_TASK_RAM_GB. 0 in the other numbers means no limit."""

    gpus: tuple[int, int] = (0, 0)
    gpu_ram_gb: float = 0
    gpu_models: list[str] = field(default_factory=list)
    min_compute_cap: float = 0
    max_compute_cap: float = 0
    cpus: int = 0
    ram_gb: float = 0
    disk_gb: float = 64
    min_down_mbps: float = 0
    min_up_mbps: float = 0
    max_price: float = 0
    regions: list[str] = field(default_factory=list)
    hours: float | None = None

    @property
    def cpu_task(self) -> bool:
        return self.gpus[1] == 0

    def least_cpus(self) -> int:
        return max(self.cpus, CPU_TASK_CPUS) if self.cpu_task else self.cpus

    def least_ram_gb(self) -> float:
        return max(self.ram_gb, CPU_TASK_RAM_GB) if self.cpu_task else self.ram_gb


def lease_hours(hours: float | None) -> float:
    """How long a host must still be available for a task of `hours` (None: not known)."""
    return max(LEASE_FACTOR * (hours or 0), MIN_LEASE_HOURS) + 1


def image_cuda(image: str) -> float | None:
    """The CUDA version in an image tag ("...cuda-12.8.1..." -> 12.8)."""
    m = re.search(r"cuda-(\d+)\.(\d+)", image)
    return float(f"{m.group(1)}.{m.group(2)}") if m else None


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


def reliability(o: dict) -> float | None:
    rel = _num(o, "reliability2")
    return _num(o, "reliability") if rel is None else rel


def compute_cap(o: dict) -> float:
    """Vast's 890 -> 8.9."""
    return (_num(o, "compute_cap") or 0) / 100


def fixed(o: dict, cfg: dict, disk_gb: float, hours: float | None, now: float) -> bool:
    """The rules every offer passes, browsed or named by a launch: rentable now, a verified host at the reliability
    setting, a direct port, the download setting, room for the disk, a driver for the image's CUDA, not a mining
    card (weak at the fp16 and bf16 work it seems to offer), a country where Claude works and the regions setting
    allows, a lease long enough."""
    price, rel, cuda, end = (
        _num(o, "dph_total"),
        reliability(o),
        image_cuda(cfg["image"]),
        _num(o, "end_date"),
    )
    return (
        o.get("rentable") is not False
        and o.get("rented") is not True
        and o.get("external") is not True
        and o.get("verification", "verified") == "verified"
        and bool(price and price > 0)
        and rel is not None
        and rel >= cfg["min_reliability"]
        and (_num(o, "direct_port_count") or 0) >= 1
        and (_num(o, "inet_down") or 0) >= cfg["min_download_mbps"]
        and (_num(o, "disk_space") or 0) >= disk_gb
        and (cuda is None or (_num(o, "cuda_max_good") or 0) >= cuda)
        and not str(o.get("gpu_name") or "").upper().startswith("CMP")
        and country(o) not in BLOCKED  # (an unknown country stays allowed)
        and (not end or end >= now + 3600 * lease_hours(hours))
        and (not cfg["regions"] or country(o) in allowed_countries(cfg["regions"]))
    )


def fits(o: dict, f: Filters) -> bool:
    """The client-side check of the filters (Vast's own filter is not always exact)."""
    ok = (
        (_num(o, "cpu_cores_effective") or 0) >= f.least_cpus()
        and (_num(o, "cpu_ram") or 0) >= f.least_ram_gb() * 1000
        and (_num(o, "inet_down") or 0) >= f.min_down_mbps
        and (_num(o, "inet_up") or 0) >= f.min_up_mbps
        and (not f.max_price or o["dph_total"] <= f.max_price)
        and (not f.regions or country(o) in allowed_countries(f.regions))
    )
    if ok and not f.cpu_task:
        name, cc = str(o.get("gpu_name") or "").lower(), compute_cap(o)
        ok = (
            f.gpus[0] <= (_num(o, "num_gpus") or 0) <= f.gpus[1]
            and (_num(o, "gpu_ram") or 0) >= f.gpu_ram_gb * 1000
            and cc >= f.min_compute_cap
            and (not f.max_compute_cap or cc <= f.max_compute_cap)
            and (not f.gpu_models or any(m.lower() in name for m in f.gpu_models))
        )
    return ok


def query(f: Filters, cfg: dict) -> dict:
    """Vast's server-side filter (it counts 1 GB as 1000 MB); prices come with the disk asked for."""
    q: dict = {
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "verified": {"eq": True},
        "external": {"eq": False},
        "reliability2": {"gte": cfg["min_reliability"]},
        "direct_port_count": {"gte": 1},
        "inet_down": {"gte": max(cfg["min_download_mbps"], f.min_down_mbps)},
        "disk_space": {"gte": f.disk_gb},
        "cpu_cores_effective": {"gte": f.least_cpus()},
        "cpu_ram": {"gte": f.least_ram_gb() * 1000},
        "type": "on-demand",
        "order": [["dph_total", "asc"]],
        "limit": 3000,
        "allocated_storage": f.disk_gb,
    }
    if cuda := image_cuda(cfg["image"]):
        q["cuda_max_good"] = {"gte": cuda}
    if f.min_up_mbps:
        q["inet_up"] = {"gte": f.min_up_mbps}
    if not f.cpu_task:
        q["num_gpus"] = {"gte": f.gpus[0], "lte": f.gpus[1]}
        q["gpu_ram"] = {"gte": f.gpu_ram_gb * 1000}
    return q


def search(f: Filters, cfg: dict) -> list[dict]:
    """The offers that fit, cheapest first."""
    now = time.time()
    found = [o for o in vast.search(query(f, cfg)) if fixed(o, cfg, f.disk_gb, f.hours, now) and fits(o, f)]
    return sorted(found, key=lambda o: (o["dph_total"], o["id"]))


def named(ids: list[int], disk_gb: float, cfg: dict) -> dict[int, dict]:
    """The offers with these ids that can still be rented and pass the fixed rules, by id, priced with the disk."""
    q = {"ask_contract_id": {"in": ids}, "type": "on-demand", "allocated_storage": disk_gb, "limit": len(ids)}
    now = time.time()
    return {o["id"]: o for o in vast.search(q) if o.get("id") in ids and fixed(o, cfg, disk_gb, None, now)}


def shown(o: dict, now: float) -> dict:
    """What `offers` shows of an offer (and prints as JSON). Vast counts GPU memory in MiB, RAM in MB."""
    end = _num(o, "end_date")
    return {
        "id": o["id"],
        "machine": o.get("machine_id"),
        "usd_per_hour": round(o["dph_total"], 3),
        "gpus": int(_num(o, "num_gpus") or 0),
        "gpu": core.ascii_text(str(o.get("gpu_name") or "")),
        "gpu_ram_gb": round((_num(o, "gpu_ram") or 0) / 1024, 1),
        "compute_cap": compute_cap(o),
        "gpu_mem_bw_gbps": _num(o, "gpu_mem_bw"),
        "pcie_gbps": _num(o, "pcie_bw"),
        "nvlink": bool(_num(o, "bw_nvlink")),
        "vcpus": _num(o, "cpu_cores_effective"),
        "host_vcpus": _num(o, "cpu_cores"),
        "cpu": core.ascii_text(str(o.get("cpu_name") or "").strip()),
        "ram_gb": round((_num(o, "cpu_ram") or 0) / 1000),
        "disk_gb": round(_num(o, "disk_space") or 0),
        "disk_mbps": round(_num(o, "disk_bw") or 0),
        "down_mbps": round(_num(o, "inet_down") or 0),
        "up_mbps": round(_num(o, "inet_up") or 0),
        "down_usd_per_tb": round(_num(o, "internet_down_cost_per_tb") or 0, 2),
        "up_usd_per_tb": round(_num(o, "internet_up_cost_per_tb") or 0, 2),
        "reliability": reliability(o),
        "location": core.ascii_text(str(o.get("geolocation") or "")),
        "lease_days": round((end - now) / 86400, 1) if end else None,
    }


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
        "summary": summary(o),
        "short": short(o),
    }
