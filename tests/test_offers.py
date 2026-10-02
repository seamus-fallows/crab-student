"""offers: the search query, the fixed rules, the browsing filters, the host's lease and the offers a launch
names."""

import time

import pytest
from fake_vast import cpu_offer, offer

from crab_student import core, offers
from crab_student.offers import Filters


def cfg(**changes) -> dict:
    return {**core.load_config(), **changes}


def ids(found) -> list[int]:
    return sorted(o["id"] for o in (found.values() if isinstance(found, dict) else found))


def test_the_query_counts_a_gb_as_1000_mb_and_asks_for_gpus_only_for_gpu_tasks():
    q = offers.query(Filters(cpus=32, ram_gb=100, disk_gb=60, min_down_mbps=300), cfg(min_reliability=0.99))
    assert q["cpu_cores_effective"] == {"gte": 32} and q["cpu_ram"] == {"gte": 100000}
    assert q["disk_space"] == {"gte": 60} and q["allocated_storage"] == 60  # (so prices include the disk)
    assert q["inet_down"] == {"gte": 300} and q["reliability2"] == {"gte": 0.99}
    assert q["direct_port_count"] == {"gte": 1} and q["cuda_max_good"] == {"gte": 12.8}
    assert not {"num_gpus", "gpu_ram", "inet_up"} & q.keys()
    q = offers.query(Filters(gpus=(2, 4), gpu_ram_gb=24, min_up_mbps=200), cfg())
    assert (q["num_gpus"], q["gpu_ram"], q["inet_up"]) == ({"gte": 2, "lte": 4}, {"gte": 24000}, {"gte": 200})
    assert "cuda_max_good" not in offers.query(Filters(), cfg(image="ubuntu:24.04"))


def test_a_task_that_uses_no_gpu_gets_at_least_the_cpu_task_minimum():
    q = offers.query(Filters(cpus=8, ram_gb=16), cfg())
    assert q["cpu_cores_effective"] == {"gte": 24} and q["cpu_ram"] == {"gte": 64000}
    q = offers.query(Filters(gpus=(1, 1), cpus=8, ram_gb=16), cfg())  # (a GPU task asks for what it needs)
    assert q["cpu_cores_effective"] == {"gte": 8} and q["cpu_ram"] == {"gte": 16000}


def test_the_fixed_rules_and_the_filters_are_checked_again_on_our_side(fake_vast):
    fake_vast.ignore_filters = True  # (Vast's own filter is not always exact)
    now = time.time()
    fake_vast.offers = [
        offer(1),
        offer(2, end_date=now + 26 * 3600),
        offer(3, reliability2=None, reliability=0.99),
        offer(10, cpu_ram=15999),  # 16 GB is 16000 MB
        offer(11, verification="unverified", verified=False),
        offer(12, rentable=False),
        offer(13, rented=True),
        offer(14, external=True),
        offer(15, reliability2=0.97),
        offer(16, num_gpus=2),
        offer(17, gpu_ram=23999),
        offer(18, cuda_max_good=12.4),  # older than the image's CUDA 12.8
        offer(19, end_date=now + 24 * 3600),  # gone before a day (+ 1 h) is up
        offer(20, direct_port_count=0),
        offer(21, inet_down=199),  # (the min_download_mbps setting)
        offer(22, disk_space=49),
        offer(23, cpu_cores_effective=7),
        offer(24, dph_total=0),
        offer(25, gpu_name="CMP 170HX"),  # a mining card
    ]
    f = Filters(gpus=(1, 1), gpu_ram_gb=24, cpus=8, ram_gb=16, disk_gb=50)
    assert ids(offers.search(f, cfg())) == [1, 2, 3]


def test_gpu_counts_models_and_generations(fake_vast):
    fake_vast.offers = [
        offer(1, gpu_name="RTX 4090"),
        offer(2, gpu_name="A100 SXM4", compute_cap=800, num_gpus=2),
        offer(3, gpu_name="RTX 3090", compute_cap=860, num_gpus=4),
        offer(4, gpu_name="RTX 5090", compute_cap=1200),
        offer(5, gpu_name="Tesla V100", compute_cap=700),
    ]
    assert ids(offers.search(Filters(gpus=(1, 8)), cfg())) == [1, 2, 3, 4, 5]
    assert ids(offers.search(Filters(gpus=(2, 4)), cfg())) == [2, 3]
    assert ids(offers.search(Filters(gpus=(1, 8), gpu_models=["4090", "a100"]), cfg())) == [1, 2]
    assert ids(offers.search(Filters(gpus=(1, 8), min_compute_cap=8.0, max_compute_cap=9.0), cfg())) == [
        1,
        2,
        3,
    ]


def test_a_task_that_uses_no_gpu_runs_on_any_machine_with_enough_cpu(fake_vast):
    fake_vast.offers = [
        cpu_offer(1),
        cpu_offer(2, num_gpus=4),
        offer(3),
        cpu_offer(4, cpu_cores_effective=23),
    ]
    assert ids(offers.search(Filters(), cfg())) == [1, 2]  # (offer 3: 16 vCPUs)


def test_price_network_and_regions(fake_vast):
    fake_vast.offers = [
        offer(1, dph_total=0.30, geolocation="Quebec, CA"),
        offer(2, dph_total=0.90, geolocation="Bavaria, DE", inet_up=100),
        offer(3, dph_total=0.50, geolocation="", inet_down=2000),
    ]
    g = (1, 1)
    assert ids(offers.search(Filters(gpus=g, max_price=0.6), cfg())) == [1, 3]
    assert ids(offers.search(Filters(gpus=g, min_up_mbps=200), cfg())) == [1, 3]
    assert ids(offers.search(Filters(gpus=g, min_down_mbps=1000), cfg())) == [3]
    assert ids(offers.search(Filters(gpus=g, regions=["europe"]), cfg())) == [2]
    assert [o["id"] for o in offers.search(Filters(gpus=g), cfg())] == [1, 3, 2]  # (cheapest first)


def test_the_regions_setting_allows_country_codes_and_continents(fake_vast):
    places = ["Quebec, CA", "Bavaria, DE", "California, US", "", None, "Somewhere"]
    fake_vast.offers = [offer(i, geolocation=place) for i, place in enumerate(places, 1)]
    assert ids(offers.search(Filters(gpus=(1, 1)), cfg(regions=["ca", "Europe"]))) == [1, 2]
    assert len(offers.search(Filters(gpus=(1, 1)), cfg())) == 6  # (empty means anywhere)


@pytest.mark.parametrize("regions", [[], ["asia", "europe", "CN", "HK", "RU"]])
def test_hosts_where_claude_is_unavailable_never_fit(fake_vast, regions):
    blocked = ["Guangdong, CN", "Hong Kong, HK", "Macau, MO", "Moscow, RU", "Minsk, BY", "Tehran, IR",
               "Damascus, SY", "Havana, CU", "Caracas, VE", "Pyongyang, KP"]  # fmt: skip
    allowed = ["Tokyo, JP", "Bavaria, DE", "Taipei, TW", "", None, "Somewhere"]
    fake_vast.offers = [offer(i, geolocation=place) for i, place in enumerate(blocked + allowed, 1)]
    found = offers.search(Filters(gpus=(1, 1)), cfg(regions=regions))
    assert {offers.country(o) for o in found} <= {"JP", "DE", "TW", None}
    assert len(found) == (6 if not regions else 3)  # (unknown places stay allowed, as regions permits)
    named = offers.named([o["id"] for o in fake_vast.offers], 64, cfg(regions=regions))
    assert len(named) == len(found)  # (a launch's own offers pass the same rules)


@pytest.mark.parametrize("hours, needed", [(None, 25), (3, 25), (20, 41)])
def test_the_host_must_stay_twice_the_hours_and_at_least_a_day(hours, needed, fake_vast):
    now = time.time()
    fake_vast.offers = [
        offer(1, end_date=now + (needed - 0.1) * 3600),
        offer(2, end_date=now + (needed + 0.1) * 3600),
    ]
    assert ids(offers.search(Filters(gpus=(1, 1), hours=hours), cfg())) == [2]


def test_a_launch_names_offers_by_id_and_gets_those_still_to_be_had(fake_vast):
    fake_vast.offers = [offer(1), offer(2, rentable=False), offer(3, cuda_max_good=11.0), offer(4), offer(5)]
    found = offers.named([4, 1, 2, 3, 9], 80, cfg())
    assert ids(found) == [1, 4]  # (2 is taken, 3 fails the fixed rules, 9 is gone)
    (_, _, body), *_ = fake_vast.calls("POST", "/api/v0/bundles/")
    assert body["ask_contract_id"] == {"in": [4, 1, 2, 3, 9]} and body["allocated_storage"] == 80
    fake_vast.offers = [offer(1, disk_space=50)]
    assert offers.named([1], 80, cfg()) == {}  # (no room for the disk)


def test_what_offers_shows_and_a_record_keeps_is_ascii():
    now = time.time()
    v = offers.shown(offer(1, geolocation="Zürich, CH", end_date=now + 3 * 86400, gpu_name="RTX 4090™"), now)
    assert (v["id"], v["machine"], v["gpus"], v["gpu_ram_gb"], v["compute_cap"]) == (1, 10, 1, 24.0, 8.9)
    assert (v["vcpus"], v["host_vcpus"], v["ram_gb"], v["lease_days"]) == (16, 64, 64, 3.0)
    assert all(isinstance(x, str) and x.isascii() for x in (v["gpu"], v["cpu"], v["location"]))
    kept = offers.kept(offer(1, geolocation="Zürich, CH"))
    assert (kept["id"], kept["machine_id"], kept["dph_total"]) == (1, 10, 0.40)
    assert all(part in kept["summary"] for part in ("1x RTX 4090", "24 GB", "16 cores", "64 GB RAM"))
    assert "RTX 4090" in kept["short"] and kept["summary"].isascii() and kept["short"].isascii()
