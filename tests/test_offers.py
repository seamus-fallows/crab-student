"""offers: the search query, the client-side recheck, the host's lease and the picking rule."""

import time

import pytest
from fake_vast import cpu_offer, offer

from crab_student import core, offers
from crab_student.offers import Req


def cfg(**changes) -> dict:
    return {**core.load_config(), **changes}


def ids(found: list[dict]) -> list[int]:
    return [o["id"] for o in found]


def test_the_query_counts_a_gb_as_1000_mb_and_asks_for_gpus_only_for_gpu_tasks():
    req = Req(hours=1, cpus=8, ram_gb=16, disk_gb=60, min_down_mbps=300)
    q = offers.query(req, cfg(min_reliability=0.99))
    assert q["cpu_cores_effective"] == {"gte": 8} and q["cpu_ram"] == {"gte": 16000}
    assert q["disk_space"] == {"gte": 60} and q["allocated_storage"] == 60  # (so prices include the disk)
    assert q["inet_down"] == {"gte": 300} and q["reliability2"] == {"gte": 0.99}
    assert q["direct_port_count"] == {"gte": 1}
    assert not {"num_gpus", "gpu_ram", "cuda_max_good"} & q.keys()
    q = offers.query(Req(hours=1, gpus=2, gpu_ram_gb=24), cfg())
    assert (q["num_gpus"], q["gpu_ram"], q["cuda_max_good"]) == ({"eq": 2}, {"gte": 24000}, {"gte": 12.8})
    assert "cuda_max_good" not in offers.query(Req(hours=1, gpus=1), cfg(image="ubuntu:24.04"))


def test_every_requirement_is_checked_again_on_our_side(fake_vast):
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
        offer(21, inet_down=199),
        offer(22, disk_space=49),
        offer(23, cpu_cores_effective=7),
        offer(24, dph_total=0),
    ]
    req = Req(hours=2, gpus=1, gpu_ram_gb=24, cpus=8, ram_gb=16, disk_gb=50, min_down_mbps=200)
    assert sorted(ids(offers.search(req, cfg()))) == [1, 2, 3]


def test_gpu_models_match_any_of_them_by_substring(fake_vast):
    names = ["RTX 4090", "A100 SXM4", "RTX 3090", "H100 NVL"]
    fake_vast.offers = [offer(i, gpu_name=name) for i, name in enumerate(names, 1)]
    assert sorted(ids(offers.search(Req(hours=1, gpus=1, gpu_models=["4090", "a100"]), cfg()))) == [1, 2]


def test_regions_allow_country_codes_and_continents(fake_vast):
    places = ["Quebec, CA", "Bavaria, DE", "California, US", "", None, "Somewhere"]
    fake_vast.offers = [offer(i, geolocation=place) for i, place in enumerate(places, 1)]
    assert sorted(ids(offers.search(Req(hours=1), cfg(regions=["ca", "Europe"])))) == [1, 2]
    assert len(offers.search(Req(hours=1), cfg())) == 6  # (empty means anywhere)


def test_a_cpu_task_has_no_gpu_conditions(fake_vast):
    fake_vast.offers = [cpu_offer(1), offer(2, gpu_name="GTX 1080", gpu_ram=8192, cuda_max_good=11.0)]
    assert sorted(ids(offers.search(Req(hours=1), cfg()))) == [1, 2]


def test_a_cpu_task_takes_the_cheapest():
    found = [cpu_offer(1, dph_total=0.30), cpu_offer(2, dph_total=0.10), cpu_offer(3, dph_total=0.20)]
    assert ids(offers.rank(found, gpu=False)) == [2, 3, 1]


def test_a_gpu_task_pays_more_only_for_clearly_better_speed_per_dollar():
    cheap = offer(1, dph_total=0.40, dlperf=40)  # 100 per $
    clearly = offer(2, dph_total=0.55, dlperf=100)  # 182 per $, within 1.5x the price
    marginal = offer(3, dph_total=0.50, dlperf=55)  # 110 per $
    dear = offer(4, dph_total=0.70, dlperf=300)  # 429 per $, but over 1.5x the price
    assert offers.rank([cheap, clearly, dear], gpu=True)[0] is clearly
    assert offers.rank([cheap, marginal, dear], gpu=True)[0] is cheap
    assert offers.rank([cheap, clearly], gpu=False)[0] is cheap


def test_among_similar_prices_reliability_and_download_speed_decide_together():
    found = [
        cpu_offer(1, dph_total=0.40, inet_down=300, reliability2=0.99),
        cpu_offer(2, dph_total=0.47, inet_down=900, reliability2=0.99),  # within 20%: the faster download
        cpu_offer(3, dph_total=0.49, inet_down=5000, reliability2=0.999),  # over 20% dearer than the cheapest
    ]
    assert ids(offers.rank(found, gpu=False)) == [2, 1, 3]
    # A point of reliability weighs as much as twice the download speed.
    steady, fast = (
        cpu_offer(1, inet_down=400, reliability2=0.995),
        cpu_offer(2, inet_down=1000, reliability2=0.98),
    )
    assert ids(offers.rank([steady, fast], gpu=False)) == [1, 2]
    fast["inet_down"] = 4000
    assert ids(offers.rank([steady, fast], gpu=False)) == [2, 1]


def test_ties_are_broken_the_same_way_every_time():
    same = [offer(i, dph_total=0.40) for i in (5, 3, 9)]
    assert ids(offers.rank(same, gpu=True)) == ids(offers.rank(same[::-1], gpu=True)) == [3, 5, 9]


def test_fallbacks_are_up_to_two_other_machines_at_most_a_quarter_dearer(fake_vast):
    fake_vast.offers = [
        cpu_offer(1, machine_id=10, dph_total=0.40),
        cpu_offer(2, machine_id=10, dph_total=0.41),  # the pick's machine
        cpu_offer(3, machine_id=20, dph_total=0.45),
        cpu_offer(4, machine_id=20, dph_total=0.46),  # the first fallback's machine
        cpu_offer(5, machine_id=30, dph_total=0.49),
        cpu_offer(6, machine_id=40, dph_total=0.50),  # a third
    ]
    p = offers.plan(Req(hours=1), cfg())
    assert (p.primary["id"], ids(p.fallbacks), len(p.ranked)) == (1, [3, 5], 6)
    fake_vast.offers = [cpu_offer(1, dph_total=0.40), cpu_offer(2, dph_total=0.51)]
    assert offers.plan(Req(hours=1), cfg()).fallbacks == []  # over 1.25x


@pytest.mark.parametrize("hours, needed", [(None, 25), (3, 25), (20, 41)])
def test_the_host_must_stay_twice_the_hours_and_at_least_a_day(hours, needed, fake_vast):
    now = time.time()
    fake_vast.offers = [
        offer(1, end_date=now + (needed - 0.1) * 3600),
        offer(2, end_date=now + (needed + 0.1) * 3600),
    ]
    assert ids(offers.search(Req(hours=hours), cfg())) == [2]


def test_offers_up_to_the_cap_come_first_and_the_rest_only_over_the_cap(fake_vast):
    fake_vast.offers = [
        cpu_offer(1, dph_total=0.40, inet_down=300),
        cpu_offer(2, dph_total=0.43, inet_down=900),
    ]
    assert offers.plan(Req(hours=5), cfg()).primary["id"] == 2  # the faster download, without a cap
    p = offers.plan(Req(hours=5), cfg(), cap=0.41)
    assert (p.primary["id"], p.fallbacks, p.over_cap) == (1, [], False)  # (offer 2 is no backup: over it)
    p = offers.plan(Req(hours=5), cfg(), cap=0.30)
    assert (p.primary["id"], p.over_cap, p.top_price()) == (2, True, 0.43)


def test_offers_already_taken_are_left_out(fake_vast):
    fake_vast.offers = [cpu_offer(1, dph_total=0.40), cpu_offer(2, dph_total=0.41)]
    assert offers.plan(Req(hours=1), cfg(), taken={1}).offers() == [
        offers.plan(Req(hours=1), cfg()).fallbacks[0]
    ]
    with pytest.raises(core.CrabError) as e:
        offers.plan(Req(hours=1), cfg(), taken={1, 2})
    assert e.value.code == 4


@pytest.mark.parametrize("cap", [None, 100.0])
def test_nothing_fitting_the_hardware_is_exit_4(fake_vast, cap):
    fake_vast.offers = [cpu_offer(1, cpu_ram=8000), offer(2, geolocation="California, US")]
    with pytest.raises(core.CrabError) as e:
        offers.plan(Req(hours=1, ram_gb=16), cfg(regions=["CA"]), cap)
    assert e.value.code == 4


def test_what_a_record_keeps_of_an_offer_is_short_and_ascii():
    gpu = offers.kept(offer(1, geolocation="Zürich, CH"))
    assert (gpu["id"], gpu["machine_id"], gpu["dph_total"], gpu["gpu_host"]) == (1, 10, 0.40, True)
    assert all(part in gpu["summary"] for part in ("1x RTX 4090", "24 GB", "16 cores", "64 GB RAM"))
    assert "RTX 4090" in gpu["short"] and gpu["summary"].isascii() and gpu["short"].isascii()
    cpu = offers.kept(cpu_offer(2))
    assert cpu["gpu_host"] is False and "0x" not in cpu["summary"] and "Quebec, CA" in cpu["short"]
