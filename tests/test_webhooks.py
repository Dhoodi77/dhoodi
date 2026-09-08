"""Helius webhooks: registration sync, delivery handling, endpoint auth.

Plus the automatic-fix additions: provider health checks, discovery
flywheel, and signal-performance reporting.
"""
import json
import time

import pytest

from tradeos.learning.post_trade import signal_performance
from tradeos.wallets.reputation import WalletPerformance, WalletReputationStore
from tradeos.wallets.scanner import SmartMoneyScanner
from tradeos.wallets.webhooks import HeliusWebhookManager

from .conftest import make_settings
from .test_helius import MINT, WALLET, buy_tx, sell_tx

GOOD_PERF = WalletPerformance(trade_count=40, win_rate=0.9, avg_return_pct=60,
                              return_stddev_pct=20)


class StubHeliusWebhooks:
    """Captures webhook CRUD calls; serves canned webhook lists."""

    def __init__(self, existing=None, fail_list=False):
        self.existing = existing if existing is not None else []
        self.fail_list = fail_list
        self.created: list[dict] = []
        self.updated: list[dict] = []

    async def list_webhooks(self):
        return None if self.fail_list else self.existing

    async def create_webhook(self, url, addresses, auth_header):
        self.created.append({"url": url, "addresses": addresses,
                             "auth": auth_header})
        return {"webhookID": "wh_new"}

    async def update_webhook(self, webhook_id, url, addresses, auth_header):
        self.updated.append({"id": webhook_id, "addresses": addresses})
        return {"webhookID": webhook_id}


def make_manager(db, helius=None, *, public_url="https://vps.example.com",
                 secret="whsec-test", pipeline=None, market=None):
    settings = make_settings(public_url=public_url,
                             helius_webhook_secret=secret)
    reputation = WalletReputationStore(db)
    scanner = SmartMoneyScanner(settings, db, helius, reputation)
    return HeliusWebhookManager(settings, db, helius or StubHeliusWebhooks(),
                                reputation, scanner, pipeline=pipeline,
                                market=market), reputation


# --- registration sync ----------------------------------------------------

async def test_sync_creates_webhook_for_tracked_wallets(db):
    helius = StubHeliusWebhooks()
    manager, reputation = make_manager(db, helius)
    reputation.record_score("solana", "smartW", GOOD_PERF)

    assert await manager.sync() is True
    assert len(helius.created) == 1
    assert helius.created[0]["url"] == "https://vps.example.com/webhooks/helius"
    assert helius.created[0]["addresses"] == ["smartW"]
    assert helius.created[0]["auth"] == "whsec-test"
    assert db.kv_get("helius_webhook_id") == "wh_new"


async def test_sync_updates_when_wallet_set_changes(db):
    helius = StubHeliusWebhooks(existing=[{
        "webhookID": "wh_1",
        "webhookURL": "https://vps.example.com/webhooks/helius",
        "accountAddresses": ["oldW"]}])
    manager, reputation = make_manager(db, helius)
    reputation.record_score("solana", "smartW", GOOD_PERF)

    assert await manager.sync() is True
    assert helius.created == []
    assert helius.updated[0]["id"] == "wh_1"
    assert helius.updated[0]["addresses"] == ["smartW"]


async def test_sync_noop_when_current(db):
    helius = StubHeliusWebhooks(existing=[{
        "webhookID": "wh_1",
        "webhookURL": "https://vps.example.com/webhooks/helius",
        "accountAddresses": ["smartW"]}])
    manager, reputation = make_manager(db, helius)
    reputation.record_score("solana", "smartW", GOOD_PERF)
    assert await manager.sync() is True
    assert helius.created == [] and helius.updated == []


async def test_sync_disabled_without_config(db):
    manager, reputation = make_manager(db, public_url=None, secret=None)
    reputation.record_score("solana", "smartW", GOOD_PERF)
    assert await manager.sync() is False


async def test_sync_skips_with_no_tracked_wallets(db):
    helius = StubHeliusWebhooks()
    manager, _ = make_manager(db, helius)
    assert await manager.sync() is False
    assert helius.created == []


async def test_sync_survives_api_failure(db):
    helius = StubHeliusWebhooks(fail_list=True)
    manager, reputation = make_manager(db, helius)
    reputation.record_score("solana", "smartW", GOOD_PERF)
    assert await manager.sync() is False
    events = db.query("SELECT * FROM system_events WHERE kind = 'webhook_sync_failed'")
    assert len(events) == 1


# --- delivery -------------------------------------------------------------

async def test_payload_stores_swaps_and_ignores_untracked(db):
    manager, _ = make_manager(db)
    result = await manager.handle_payload([buy_tx(), {"type": "TRANSFER"}])
    assert result == {"received": 2, "parsed": 1, "stored": 1, "reactions": 0}
    rows = db.query("SELECT * FROM wallet_swaps")
    assert len(rows) == 1 and rows[0]["wallet"] == WALLET


class ReactionMarket:
    def __init__(self):
        self.queried: list[str] = []

    async def get_token_pairs(self, chain, mint):
        self.queried.append(mint)
        return []  # no market data -> analysis stops there, honestly


async def test_tracked_wallet_buy_alerts_and_triggers_analysis(db):
    market = ReactionMarket()
    manager, reputation = make_manager(db, market=market)
    reputation.record_score("solana", WALLET, GOOD_PERF)
    manager.market = market

    class StubPipeline:
        async def process(self, pair):  # pragma: no cover - market returns []
            raise AssertionError("should not reach pipeline without pairs")

    manager.pipeline = StubPipeline()
    result = await manager.handle_payload([buy_tx()])
    assert result["reactions"] == 1
    alerts = db.query("SELECT * FROM alerts WHERE priority = 'high'")
    assert any("Smart money bought" in a["title"] for a in alerts)
    assert market.queried == [MINT]
    audit = db.query("SELECT * FROM audit_log WHERE action = 'smartmoney_buy_event'")
    assert len(audit) == 1


async def test_tracked_wallet_sell_of_held_token_is_critical(db):
    manager, reputation = make_manager(db)
    reputation.record_score("solana", WALLET, GOOD_PERF)
    now = time.time()
    db.execute(
        "INSERT INTO positions (chain, token_address, symbol, mode, status, "
        "entry_price_usd, quantity, cost_usd, opened_at, updated_at) "
        "VALUES ('solana', ?, 'X', 'paper', 'open', 1.0, 10, 10, ?, ?)",
        (MINT, now, now))
    result = await manager.handle_payload([sell_tx()])
    assert result["reactions"] == 1
    alerts = db.query("SELECT * FROM alerts WHERE priority = 'critical'")
    assert any("SOLD a token we hold" in a["title"] for a in alerts)


async def test_duplicate_deliveries_deduplicated(db):
    manager, _ = make_manager(db)
    await manager.handle_payload([buy_tx()])
    result = await manager.handle_payload([buy_tx()])
    assert result["stored"] == 0
    assert len(db.query("SELECT * FROM wallet_swaps")) == 1


# --- endpoint auth --------------------------------------------------------

@pytest.fixture
def webhook_client(monkeypatch):
    from fastapi.testclient import TestClient

    from tradeos.api.server import create_server
    from tradeos.app import build_app

    monkeypatch.setenv("TRADEOS_HELIUS_API_KEY", "fake-key-endpoint-test")
    settings = make_settings(public_url="https://vps.example.com",
                             helius_webhook_secret="whsec-endpoint")
    app_state = build_app(settings)
    with TestClient(create_server(app_state), raise_server_exceptions=False) as c:
        yield c, app_state
    app_state.db.close()


def test_endpoint_rejects_missing_and_wrong_secret(webhook_client):
    c, _ = webhook_client
    assert c.post("/webhooks/helius", json=[]).status_code == 401
    assert c.post("/webhooks/helius", json=[],
                  headers={"Authorization": "wrong"}).status_code == 401


def test_endpoint_rejects_non_list(webhook_client):
    c, _ = webhook_client
    r = c.post("/webhooks/helius", json={"not": "a list"},
               headers={"Authorization": "whsec-endpoint"})
    assert r.status_code == 400


def test_endpoint_accepts_and_stores(webhook_client):
    c, state = webhook_client
    r = c.post("/webhooks/helius", json=[buy_tx()],
               headers={"Authorization": "whsec-endpoint"})
    assert r.status_code == 200
    assert r.json()["stored"] == 1
    assert len(state.db.query("SELECT * FROM wallet_swaps")) == 1


def test_endpoint_404_when_not_configured(monkeypatch):
    from fastapi.testclient import TestClient

    from tradeos.api.server import create_server
    from tradeos.app import build_app

    monkeypatch.delenv("TRADEOS_HELIUS_API_KEY", raising=False)
    app_state = build_app(make_settings())  # no key -> no webhook manager
    with TestClient(create_server(app_state), raise_server_exceptions=False) as c:
        assert c.post("/webhooks/helius", json=[]).status_code == 404
    app_state.db.close()


# --- discovery flywheel ---------------------------------------------------

def test_flywheel_surfaces_smart_money_buys(db):
    settings = make_settings()
    reputation = WalletReputationStore(db)
    scanner = SmartMoneyScanner(settings, db, None, reputation)
    reputation.record_score("solana", "smartW", GOOD_PERF)
    now = time.time()
    db.execute(
        "INSERT INTO wallet_swaps (chain, wallet, signature, token_mint, direction, "
        "token_amount, sol_amount, counter_mint, block_time, recorded_at) "
        "VALUES ('solana', 'smartW', 'sig1', 'NewMint111', 'buy', 100, 5, 'wsol', ?, ?)",
        (now, now))
    db.execute(
        "INSERT INTO wallet_swaps (chain, wallet, signature, token_mint, direction, "
        "token_amount, sol_amount, counter_mint, block_time, recorded_at) "
        "VALUES ('solana', 'randomW', 'sig2', 'OtherMint', 'buy', 100, 5, 'wsol', ?, ?)",
        (now, now))
    candidates = scanner.discover_candidate_mints()
    assert candidates == ["NewMint111"]  # untracked wallet's buy not surfaced

    # already-analyzed tokens don't come back
    db.execute(
        "INSERT INTO opportunities (id, chain, token_address, source, status, "
        "created_at, updated_at) VALUES ('opp_f', 'solana', 'NewMint111', 'test', "
        "'rejected', ?, ?)", (now, now))
    assert scanner.discover_candidate_mints() == []


# --- signal performance ---------------------------------------------------

def test_signal_performance_buckets(db):
    now = time.time()
    cases = [
        ("opp_a", 80, 70, None, 5.0),    # high momentum, smart money, win
        ("opp_b", 80, None, 30, -3.0),   # high momentum, whale distribution, loss
        ("opp_c", 40, None, None, -2.0), # low momentum, no signals, loss
    ]
    for opp_id, mom, smart, whale, pnl in cases:
        db.execute(
            "INSERT INTO opportunities (id, chain, token_address, source, status, "
            "momentum_score, smart_money_score, whale_score, created_at, updated_at) "
            "VALUES (?, 'solana', ?, 'test', 'executed', ?, ?, ?, ?, ?)",
            (opp_id, f"mint_{opp_id}", mom, smart, whale, now, now))
        db.execute(
            "INSERT INTO post_trade_reviews (position_id, opportunity_id, review_json, "
            "pnl_usd, followed_strategy, followed_risk_rules, created_at) "
            "VALUES (1, ?, '{}', ?, 1, 1, ?)", (opp_id, pnl, now))

    report = signal_performance(db)
    assert report["closed_trades"] == 3
    buckets = {b["bucket"]: b for b in report["buckets"]}
    assert buckets["momentum>=65"]["trades"] == 2
    assert buckets["momentum>=65"]["wins"] == 1
    assert buckets["smart_money_present"]["trades"] == 1
    assert buckets["smart_money_present"]["win_rate"] == 1.0
    assert buckets["no_onchain_signals"]["trades"] == 1
    assert all(b["low_confidence"] for b in report["buckets"])


# --- provider health checks ----------------------------------------------

async def test_helius_health_check_detects_drift(monkeypatch):
    from tradeos.providers.chains.helius import HeliusProvider

    provider = HeliusProvider("fake-key-health")

    async def fake_rpc(method, params=None):
        if method == "getSlot":
            return 12345
        if method == "getTokenAccounts":
            return {"unexpected_shape": True}  # drifted response
        return None

    async def fake_request(method, path, params=None, body=None, retries=3):
        return [] if path == "/v0/webhooks" else None

    monkeypatch.setattr(provider, "_rpc", fake_rpc)
    monkeypatch.setattr(provider, "_request_json", fake_request)
    health = await provider.health_check()
    assert health["rpc"] is True
    assert health["api"] is True
    assert health["das_shape"] is False
    assert health["ok"] is False
    await provider.close()


async def test_startup_health_check_alerts_on_failure(db):
    from tradeos.orchestration.scheduler import Scheduler

    class DeadMarket:
        async def search(self, q):
            raise RuntimeError("blocked")

    scheduler = Scheduler(make_settings(), db, DeadMarket(), None, None)
    await scheduler._startup_health_checks()
    health = json.loads(db.kv_get("provider_health"))
    assert health["dexscreener"]["ok"] is False
    alerts = db.query("SELECT * FROM alerts WHERE priority = 'critical'")
    assert any("health check FAILED" in a["title"] for a in alerts)
