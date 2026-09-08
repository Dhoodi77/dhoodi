"""Smart-money pipeline: round-trip math, scanner, token signals."""
import time

from tradeos.providers.chains.helius import SwapRecord
from tradeos.wallets.analysis import analyze_wallet_swaps, extract_round_trips
from tradeos.wallets.reputation import WalletPerformance, WalletReputationStore
from tradeos.wallets.scanner import SmartMoneyScanner

from .conftest import make_settings

MINT_A = "MintAAAA"
MINT_B = "MintBBBB"


def swap(mint, direction, tokens, sol, ts):
    return {"token_mint": mint, "direction": direction, "token_amount": tokens,
            "sol_amount": sol, "block_time": ts}


# --- round-trip extraction ------------------------------------------------

def test_profitable_round_trip():
    trips = extract_round_trips([
        swap(MINT_A, "buy", 1000, 2.0, 100),
        swap(MINT_A, "sell", 1000, 3.0, 200),
    ])
    assert len(trips) == 1
    assert abs(trips[0].return_pct - 50.0) < 1e-9


def test_partial_sell_uses_average_cost():
    trips = extract_round_trips([
        swap(MINT_A, "buy", 1000, 2.0, 100),
        swap(MINT_A, "sell", 500, 1.5, 200),   # half out: cost 1.0 -> +50%
        swap(MINT_A, "sell", 500, 0.5, 300),   # rest: cost 1.0 -> -50%
    ])
    assert len(trips) == 2
    assert abs(trips[0].return_pct - 50.0) < 1e-9
    assert abs(trips[1].return_pct + 50.0) < 1e-9


def test_sell_without_prior_buy_ignored():
    trips = extract_round_trips([swap(MINT_A, "sell", 1000, 5.0, 100)])
    assert trips == []


def test_oversized_sell_scales_proceeds():
    """Selling more than the observed buy only credits the matched share."""
    trips = extract_round_trips([
        swap(MINT_A, "buy", 500, 1.0, 100),
        swap(MINT_A, "sell", 1000, 4.0, 200),  # only 500 attributable -> 2.0 SOL
    ])
    assert len(trips) == 1
    assert abs(trips[0].proceeds_sol - 2.0) < 1e-9
    assert abs(trips[0].return_pct - 100.0) < 1e-9


def test_mints_tracked_independently():
    trips = extract_round_trips([
        swap(MINT_A, "buy", 100, 1.0, 100),
        swap(MINT_B, "buy", 200, 1.0, 110),
        swap(MINT_B, "sell", 200, 2.0, 120),
        swap(MINT_A, "sell", 100, 0.5, 130),
    ])
    assert len(trips) == 2
    by_mint = {t.token_mint: t.return_pct for t in trips}
    assert abs(by_mint[MINT_B] - 100.0) < 1e-9
    assert abs(by_mint[MINT_A] + 50.0) < 1e-9


def test_analyze_wallet_swaps_aggregates():
    perf = analyze_wallet_swaps([
        swap(MINT_A, "buy", 100, 1.0, 100), swap(MINT_A, "sell", 100, 2.0, 200),
        swap(MINT_B, "buy", 100, 1.0, 300), swap(MINT_B, "sell", 100, 0.8, 400),
    ])
    assert perf.trade_count == 2
    assert perf.win_rate == 0.5
    assert abs(perf.avg_return_pct - 40.0) < 1e-6  # (+100 - 20) / 2
    assert analyze_wallet_swaps([]) is None
    assert analyze_wallet_swaps([swap(MINT_A, "buy", 100, 1.0, 100)]) is None


# --- token signals --------------------------------------------------------

def _store_swap(db, wallet, mint, direction, sol, ts=None, sig=None):
    db.execute(
        "INSERT OR IGNORE INTO wallet_swaps (chain, wallet, signature, token_mint, "
        "direction, token_amount, sol_amount, counter_mint, block_time, recorded_at) "
        "VALUES ('solana', ?, ?, ?, ?, 1000, ?, 'wsol', ?, ?)",
        (wallet, sig or f"sig_{wallet}_{mint}_{direction}_{ts or time.time()}",
         mint, direction, sol, ts or time.time(), time.time()))


def test_token_signals_none_without_data(db):
    reputation = WalletReputationStore(db)
    assert reputation.token_signals("solana", MINT_A) == (None, None)


def test_token_signals_smart_money_buyers(db):
    reputation = WalletReputationStore(db)
    good = WalletPerformance(trade_count=40, win_rate=0.9, avg_return_pct=60,
                             return_stddev_pct=20)
    reputation.record_score("solana", "smartW", good)
    _store_swap(db, "smartW", MINT_A, "buy", 5.0)
    _store_swap(db, "unknownW", MINT_A, "buy", 5.0)
    smart, whale = reputation.token_signals("solana", MINT_A)
    assert smart == 65.0  # 50 + 15 for one smart buyer
    assert whale is None  # no whale-sized swaps


def test_token_signals_smart_seller_penalized(db):
    reputation = WalletReputationStore(db)
    good = WalletPerformance(trade_count=40, win_rate=0.9, avg_return_pct=60,
                             return_stddev_pct=20)
    reputation.record_score("solana", "smartW", good)
    _store_swap(db, "smartW", MINT_A, "sell", 5.0)
    smart, _ = reputation.token_signals("solana", MINT_A)
    assert smart == 40.0  # 50 - 10


def test_token_signals_unscored_wallets_give_no_smart_signal(db):
    reputation = WalletReputationStore(db)
    _store_swap(db, "randomW", MINT_A, "buy", 5.0)
    smart, _ = reputation.token_signals("solana", MINT_A)
    assert smart is None


def test_whale_score_from_flow_balance(db):
    reputation = WalletReputationStore(db)
    _store_swap(db, "whale1", MINT_A, "buy", 100.0)
    _store_swap(db, "whale2", MINT_A, "sell", 300.0)
    _, whale = reputation.token_signals("solana", MINT_A, whale_sol_threshold=50.0)
    assert whale == 25.0  # 50 + 50 * (100-300)/400
    _store_swap(db, "shrimp", MINT_A, "buy", 1.0)
    _, whale2 = reputation.token_signals("solana", MINT_A, whale_sol_threshold=50.0)
    assert whale2 == 25.0  # small swaps don't move the whale signal


def test_old_swaps_outside_window_ignored(db):
    reputation = WalletReputationStore(db)
    _store_swap(db, "whale1", MINT_A, "buy", 100.0, ts=time.time() - 48 * 3600)
    assert reputation.token_signals("solana", MINT_A, since_hours=24) == (None, None)


# --- scanner --------------------------------------------------------------

class StubHelius:
    """Serves canned swap histories per address."""

    def __init__(self, histories: dict[str, list[SwapRecord]]):
        self.histories = histories
        self.calls: list[str] = []

    async def get_address_swaps(self, address, limit=100, before=None):
        self.calls.append(address)
        return self.histories.get(address, [])


def rec(wallet, mint, direction, tokens, sol, ts, sig=None):
    return SwapRecord("solana", wallet, sig or f"{wallet}-{mint}-{direction}-{ts}",
                      mint, direction, tokens, sol, "wsol", ts)


async def test_scanner_discovers_and_scores_wallets(db):
    now = time.time()
    settings = make_settings()
    reputation = WalletReputationStore(db)
    # a token the system is watching (recent opportunity)
    db.execute(
        "INSERT INTO opportunities (id, chain, token_address, source, status, "
        "created_at, updated_at) VALUES ('opp_sm', 'solana', ?, 'test', "
        "'rejected', ?, ?)", (MINT_A, now, now))

    winner_history = [
        rec("winnerW", MINT_A, "buy", 1000, 1.0, now - 4000),
        rec("winnerW", MINT_A, "sell", 1000, 2.0, now - 3000),
        rec("winnerW", MINT_B, "buy", 500, 1.0, now - 2500, "w-b2"),
        rec("winnerW", MINT_B, "sell", 500, 1.8, now - 2000, "w-s2"),
        rec("winnerW", MINT_A, "buy", 200, 0.5, now - 1000, "w-b3"),
        rec("winnerW", MINT_A, "sell", 200, 0.9, now - 500, "w-s3"),
    ]
    helius = StubHelius({
        MINT_A: [
            rec("winnerW", MINT_A, "buy", 200, 0.5, now - 1000, "w-b3"),
            rec("oneTradeW", MINT_A, "buy", 100, 0.2, now - 900),
        ],
        "winnerW": winner_history,
        "oneTradeW": [rec("oneTradeW", MINT_A, "buy", 100, 0.2, now - 900)],
    })
    scanner = SmartMoneyScanner(settings, db, helius, reputation)
    stats = await scanner.scan()

    assert stats["tokens"] == 1
    assert stats["wallets_analyzed"] == 2
    # winnerW: 3 profitable round trips -> scored; oneTradeW: no round trip
    assert stats["wallets_scored"] == 1
    score = reputation.current_score("solana", "winnerW")
    assert score > 50
    rows = db.query("SELECT * FROM wallet_scores WHERE address = 'winnerW'")
    assert rows[0]["trade_count"] == 3
    assert rows[0]["win_rate"] == 1.0
    # dedup: signatures stored once
    n = db.query_one("SELECT COUNT(*) AS n FROM wallet_swaps "
                     "WHERE signature = 'w-b3'")["n"]
    assert n == 1


async def test_scanner_skips_recently_scored_wallets(db):
    now = time.time()
    settings = make_settings()
    reputation = WalletReputationStore(db)
    db.execute(
        "INSERT INTO opportunities (id, chain, token_address, source, status, "
        "created_at, updated_at) VALUES ('opp_sm2', 'solana', ?, 'test', "
        "'rejected', ?, ?)", (MINT_A, now, now))
    reputation.record_score("solana", "freshW",
                            WalletPerformance(10, 0.8, 30, 20))
    helius = StubHelius({MINT_A: [rec("freshW", MINT_A, "buy", 100, 1.0, now - 100)]})
    scanner = SmartMoneyScanner(settings, db, helius, reputation)
    stats = await scanner.scan()
    assert stats["wallets_analyzed"] == 0  # already scored 0 hours ago
    assert "freshW" not in helius.calls
