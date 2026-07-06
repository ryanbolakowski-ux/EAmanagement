"""Owner hard rule (2026-07-06): daily bias gates entry DIRECTION everywhere.
bullish -> shorts blocked; bearish -> longs blocked; neutral/unknown -> both."""
import asyncio
import pytest

from app.engines import bias_alignment as ba


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    ba._CACHE.clear()
    monkeypatch.setenv("DAILY_BIAS_ALIGNMENT", "1")
    yield
    ba._CACHE.clear()


def _set_bias(monkeypatch, value):
    async def fake(instrument):
        return value
    monkeypatch.setattr(ba, "get_daily_bias", fake)


def test_bullish_blocks_shorts_allows_longs(monkeypatch):
    _set_bias(monkeypatch, "bullish")
    ok, why = asyncio.run(ba.direction_allowed("NQ", "short"))
    assert ok is False and "BULLISH" in why
    ok, _ = asyncio.run(ba.direction_allowed("NQ", "long"))
    assert ok is True


def test_bearish_blocks_longs_allows_shorts(monkeypatch):
    _set_bias(monkeypatch, "bearish")
    ok, why = asyncio.run(ba.direction_allowed("ES", "long"))
    assert ok is False and "BEARISH" in why
    ok, _ = asyncio.run(ba.direction_allowed("ES", "short"))
    assert ok is True


def test_neutral_and_unknown_allow_both(monkeypatch):
    for bias in ("neutral", None):
        _set_bias(monkeypatch, bias)
        for d in ("long", "short"):
            ok, _ = asyncio.run(ba.direction_allowed("NQ", d))
            assert ok is True, f"bias={bias} dir={d}"


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("DAILY_BIAS_ALIGNMENT", "0")
    _set_bias(monkeypatch, "bullish")
    ok, why = asyncio.run(ba.direction_allowed("NQ", "short"))
    assert ok is True and "disabled" in why


def test_micro_contracts_map_to_parent(monkeypatch):
    calls = []
    async def fake_compute(db, inst):
        calls.append(inst)
        return {"intraday_bias": "bullish"}
    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    import app.engines.bias_alignment as mod
    monkeypatch.setattr("app.api.routes.dashboard._compute_daily_bias", fake_compute, raising=False)
    monkeypatch.setattr("app.database.async_session_factory", lambda: _FakeSession(), raising=False)
    bias = asyncio.run(mod.get_daily_bias("MNQ"))
    assert bias == "bullish"
    assert calls == ["NQ"], "micro must resolve parent instrument"


def test_engine_error_fails_open(monkeypatch):
    async def boom(instrument):
        raise RuntimeError("bias engine down")
    # direction_allowed calls get_daily_bias; make the INNER path fail instead:
    def raise_import(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("app.database.async_session_factory", raise_import, raising=False)
    ba._CACHE.clear()
    ok, why = asyncio.run(ba.direction_allowed("NQ", "short"))
    assert ok is True, "bias engine failure must fail OPEN"


def test_unknown_direction_not_gated(monkeypatch):
    _set_bias(monkeypatch, "bullish")
    ok, _ = asyncio.run(ba.direction_allowed("NQ", "flat"))
    assert ok is True


def test_cache_ttl(monkeypatch):
    calls = {"n": 0}
    async def fake_compute(db, inst):
        calls["n"] += 1
        return {"intraday_bias": "bearish"}
    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr("app.api.routes.dashboard._compute_daily_bias", fake_compute, raising=False)
    monkeypatch.setattr("app.database.async_session_factory", lambda: _FakeSession(), raising=False)
    asyncio.run(ba.get_daily_bias("ES"))
    asyncio.run(ba.get_daily_bias("ES"))
    assert calls["n"] == 1, "second lookup within TTL must hit cache"
