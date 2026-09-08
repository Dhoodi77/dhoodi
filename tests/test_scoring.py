"""Momentum + opportunity scoring, wallet reputation, DexScreener parsing."""
import time

from tradeos.providers.base import PairData
from tradeos.providers.dexscreener import parse_pair
from tradeos.strategies.momentum import analyze_momentum
from tradeos.strategies.scoring import ScoringConfig, safety_red_flags, score_opportunity
from tradeos.wallets.reputation import (
    WalletPerformance, classify, compute_smart_money_score, decayed_score)


def hot_pair(**kw) -> PairData:
    d = dict(chain="solana", pair_address="pairX", token_address="tokX",
             symbol="HOT", price_usd=0.01, liquidity_usd=150_000,
             volume_24h=800_000, volume_1h=90_000, volume_5m=12_000,
             price_change_5m=4.0, price_change_1h=12.0, price_change_6h=30.0,
             price_change_24h=80.0, buys_5m=40, sells_5m=15, buys_1h=300,
             sells_1h=150, pair_created_at=time.time() - 48 * 3600)
    d.update(kw)
    return PairData(**d)


def dead_pair() -> PairData:
    return PairData(chain="solana", pair_address="p", token_address="t",
                    symbol="DEAD", price_usd=0.001, liquidity_usd=3_000,
                    volume_24h=500, volume_1h=10, volume_5m=0,
                    price_change_5m=0, price_change_1h=-5, price_change_6h=-20,
                    price_change_24h=-40, buys_1h=2, sells_1h=20,
                    pair_created_at=time.time() - 600)


def test_momentum_orders_hot_above_dead():
    hot = analyze_momentum(hot_pair())
    dead = analyze_momentum(dead_pair())
    assert hot.score > dead.score
    assert 0 <= hot.score <= 100 and 0 <= dead.score <= 100


def test_momentum_handles_zero_data():
    empty = PairData(chain="x", pair_address="p", token_address="t")
    result = analyze_momentum(empty)
    assert 0 <= result.score <= 100


def test_red_flags_detect_thin_and_young():
    flags = safety_red_flags(dead_pair(), min_age_hours=1.0, min_liquidity_usd=25_000)
    assert any("age" in f for f in flags)
    assert any("liquidity" in f for f in flags)


def test_honeypot_pattern_flagged():
    pair = hot_pair(buys_1h=50, sells_1h=0, buys_5m=10, sells_5m=0)
    flags = safety_red_flags(pair, 1.0, 25_000)
    assert any("honeypot" in f for f in flags)


def test_opportunity_score_versioned_and_bounded():
    score = score_opportunity(hot_pair(), ScoringConfig(), 1.0, 25_000)
    assert score.version == "scoring-v1"
    assert 0 <= score.overall <= 100
    assert 0 <= score.risk <= 100
    assert score.red_flags == []


def test_risky_pair_scores_high_risk():
    score = score_opportunity(dead_pair(), ScoringConfig(), 1.0, 25_000)
    assert score.risk >= 50
    assert score.red_flags


# --- wallet reputation ---------------------------------------------------

def test_smart_money_needs_sample_size():
    small = compute_smart_money_score(WalletPerformance(2, 1.0, 80, 10))
    large = compute_smart_money_score(WalletPerformance(50, 1.0, 80, 10))
    assert large > small
    assert abs(small - 50) < abs(large - 50)


def test_bad_wallet_scores_below_neutral():
    bad = compute_smart_money_score(WalletPerformance(30, 0.2, -40, 60))
    assert bad < 50


def test_score_decays_toward_neutral():
    now = time.time()
    fresh = decayed_score(90, now, now)
    old = decayed_score(90, now - 28 * 86400, now)  # 2 half-lives
    assert fresh == 90
    assert 55 < old < 65  # 50 + 40*0.25 = 60


def test_classification_requires_history():
    assert classify(90, 2) == "neutral"
    assert classify(90, 20) == "smart_money"
    assert classify(20, 20) == "suspicious"


# --- dexscreener parsing -------------------------------------------------

def test_parse_pair_normalizes_real_shape():
    raw = {
        "chainId": "solana", "dexId": "raydium", "pairAddress": "PAIR1",
        "baseToken": {"address": "TOK1", "symbol": "TST", "name": "Test"},
        "priceUsd": "0.004217",
        "liquidity": {"usd": 61234.5},
        "volume": {"h24": 120000.1, "h1": 9000, "m5": 700},
        "priceChange": {"m5": 1.2, "h1": 5.5, "h6": -2.0, "h24": 40.0},
        "txns": {"m5": {"buys": 10, "sells": 4}, "h1": {"buys": 80, "sells": 60}},
        "pairCreatedAt": 1757000000000,
    }
    pair = parse_pair(raw)
    assert pair is not None
    assert pair.chain == "solana"
    assert pair.price_usd == 0.004217
    assert pair.liquidity_usd == 61234.5
    assert pair.buys_1h == 80
    assert pair.pair_created_at == 1757000000.0


def test_parse_pair_tolerates_missing_fields():
    pair = parse_pair({"chainId": "base", "pairAddress": "p",
                       "baseToken": {"address": "t"}})
    assert pair is not None
    assert pair.price_usd == 0.0
    assert pair.age_hours is None
