from __future__ import annotations

import time

from services.api.cache import CacheEntry, TTLCache


def test_cache_hit_and_miss() -> None:
    cache = TTLCache(max_entries=4, ttl_s=60)
    key = TTLCache.make_key({"model": "m", "messages": [{"role": "user", "content": "x"}]})
    assert cache.get(key) is None
    cache.put(key, CacheEntry(body=b"{}"))
    entry = cache.get(key)
    assert entry is not None
    assert entry.body == b"{}"
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 1


def test_cache_is_key_order_independent() -> None:
    a = TTLCache.make_key({"model": "m", "temperature": 0, "max_tokens": 8})
    b = TTLCache.make_key({"max_tokens": 8, "temperature": 0, "model": "m"})
    assert a == b


def test_cache_expires_entries() -> None:
    cache = TTLCache(max_entries=4, ttl_s=0.05)
    cache.put("k", CacheEntry(body=b"{}"))
    time.sleep(0.08)
    assert cache.get("k") is None


def test_cache_evicts_least_recently_used() -> None:
    cache = TTLCache(max_entries=2, ttl_s=60)
    cache.put("a", CacheEntry(body=b"a"))
    cache.put("b", CacheEntry(body=b"b"))
    cache.get("a")
    cache.put("c", CacheEntry(body=b"c"))
    assert cache.get("b") is None
    assert cache.get("a") is not None
    assert cache.stats()["evictions"] == 1

