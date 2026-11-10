from __future__ import annotations

from core.cache import TTLCache


def test_value_is_returned_until_it_expires(fake_time):
    cache: TTLCache[str] = TTLCache(monotonic=fake_time.monotonic)

    cache.set("rss:AAPL", "headlines", ttl_s=900)

    fake_time.now += 899
    assert cache.get("rss:AAPL") == "headlines"
    fake_time.now += 1
    assert cache.get("rss:AAPL") is None
    assert len(cache) == 0


def test_keys_are_independent(fake_time):
    cache: TTLCache[int] = TTLCache(monotonic=fake_time.monotonic)

    cache.set("a", 1, ttl_s=10)
    cache.set("b", 2, ttl_s=10)

    assert (cache.get("a"), cache.get("b"), cache.get("c")) == (1, 2, None)


def test_zero_ttl_means_do_not_cache(fake_time):
    cache: TTLCache[int] = TTLCache(monotonic=fake_time.monotonic)

    cache.set("quotes", 1, ttl_s=0)

    assert cache.get("quotes") is None


def test_full_cache_drops_expired_then_soonest_to_expire(fake_time):
    cache: TTLCache[int] = TTLCache(monotonic=fake_time.monotonic, max_entries=3)
    cache.set("short", 1, ttl_s=5)
    cache.set("medium", 2, ttl_s=50)
    cache.set("long", 3, ttl_s=500)

    fake_time.now += 10  # "short" has expired
    cache.set("new", 4, ttl_s=100)
    assert cache.get("short") is None
    assert len(cache) == 3

    cache.set("newer", 5, ttl_s=100)  # nothing expired: "medium" is the next to go
    assert cache.get("medium") is None
    assert cache.get("long") == 3
    assert cache.get("newer") == 5
