# tests/test_async_ttl.py
"""Tests for AsyncTTL cache: TTL expiry, eviction, coalescing, manual set/invalidate."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.utils.cache import AsyncTTL


@pytest.fixture
def cache():
    CACHE_SIZE = 3
    return AsyncTTL[str](ttl=2, max_size=CACHE_SIZE)


# --- Basic cache behaviour ---


@pytest.mark.asyncio
async def test_cache_miss_calls_fetcher(cache):
    fetcher = AsyncMock(return_value="hello")
    result = await cache.get_or_fetch("key1", fetcher)
    assert result == "hello"
    fetcher.assert_awaited_once()


@pytest.mark.asyncio
async def test_cache_hit_skips_fetcher(cache):
    fetcher = AsyncMock(return_value="hello")
    await cache.get_or_fetch("key1", fetcher)
    await cache.get_or_fetch("key1", fetcher)
    fetcher.assert_awaited_once()


@pytest.mark.asyncio
async def test_ttl_expiry_refetches():
    LARGE_CACHE_SIZE = 10
    cache = AsyncTTL[str](ttl=0, max_size=LARGE_CACHE_SIZE)  # instant expiry
    call_count = 0

    async def counting_fetcher():
        nonlocal call_count
        call_count += 1
        return f"v{call_count}"

    v1 = await cache.get_or_fetch("key", counting_fetcher)
    assert v1 == "v1"
    # With TTL=0 the entry expires immediately
    await asyncio.sleep(0.01)
    v2 = await cache.get_or_fetch("key", counting_fetcher)
    assert v2 == "v2"
    EXPECTED_CALLS = 2
    assert call_count == EXPECTED_CALLS


@pytest.mark.asyncio
async def test_max_size_eviction(cache):
    """Inserting more than max_size keys evicts the oldest."""
    CALL_COUNT = 4  # max_size=3
    for i in range(CALL_COUNT):
        await cache.get_or_fetch(f"k{i}", AsyncMock(return_value=f"v{i}"))
    # Cache should have at most 3 entries
    MAX_ENTRIES = 3
    assert len(cache._cache) <= MAX_ENTRIES


@pytest.mark.asyncio
async def test_none_not_cached(cache):
    mock_fetcher = AsyncMock(return_value=None)
    await cache.get_or_fetch("key1", mock_fetcher)
    await cache.get_or_fetch("key1", mock_fetcher)
    EXPECTED_CALLS = 2
    assert (
        mock_fetcher.call_count == EXPECTED_CALLS
    )  # called twice because None isn't cached


# --- Coalescing (thundering herd prevention) ---


@pytest.mark.asyncio
async def test_concurrent_fetches_coalesce(cache):
    """10 concurrent get_or_fetch calls for the same key → fetcher called once."""
    call_count = 0

    async def slow_fetcher():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return "result"

    CONCURRENT_CALLS = 10
    results = await asyncio.gather(
        *[cache.get_or_fetch("shared", slow_fetcher) for _ in range(CONCURRENT_CALLS)]
    )
    assert all(r == "result" for r in results)
    assert call_count == 1


# --- Manual operations ---


@pytest.mark.asyncio
async def test_set_then_get(cache):
    await cache.set("manual", "value")
    result = await cache.get("manual")
    assert result == "value"


@pytest.mark.asyncio
async def test_invalidate_removes_entry(cache):
    await cache.set("key", "value")
    await cache.invalidate("key")
    assert await cache.get("key") is None


@pytest.mark.asyncio
async def test_clear_empties_cache(cache):
    MAX_SIZE = 2
    cache = AsyncTTL(ttl=60, max_size=MAX_SIZE)
    await cache.set("k1", "v1")
    await cache.clear()
    assert await cache.get("k1") is None
