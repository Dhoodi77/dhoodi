"""Etherscan V2 provider: swap reconstruction, EVM scanner, whale signals.

Fixtures mirror Etherscan account-API row shapes. No network calls.
"""
import time

from tradeos.wallets.analysis import extract_round_trips
from tradeos.wallets.reputation import WalletReputationStore
from tradeos.wallets.scanner import SmartMoneyScanner
from tradeos.providers.chains.etherscan import (
    CHAIN_IDS, NATIVE, EtherscanProvider, EtherscanSwapSource,
    parse_wallet_swaps)
from tradeos.providers.swaps import SwapRecord

from .conftest import make_settings

WALLET = "0xWaLLet000000000000000000000000000000000abc"
W = WALLET.lower()
TOKEN = "0x1111111111111111111111111111111111111111"
TOKEN2 = "0x2222222222222222222222222222222222222222"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH_BASE = "0x4200000000000000000000000000000000000006"
POOL = "0x9999999999999999999999999999999999999999"


def token_row(tx_hash, contract, frm, to, amount, decimals=18, ts=1_757_000_000):
    return {"hash": tx_hash, "contractAddress": contract, "from": frm, "to": to,
            "value": str(int(amount * 10 ** decimals)), "tokenDecimal": str(decimals),
            "timeStamp": str(ts)}


def test_stable_buy_and_sell_reconstructed():
    tokentx = [
        # buy: wallet sends 100 USDC, receives 1000 TOKEN, same hash
        token_row("0xbuy", USDC_BASE, W, POOL, 100, decimals=6),
        token_row("0xbuy", TOKEN, POOL, W, 1000),
        # sell: wallet sends 1000 TOKEN, receives 150 USDC
        token_row("0xsell", TOKEN, W, POOL, 1000, ts=1_757_000_500),
        token_row("0xsell", USDC_BASE, POOL, W, 150, decimals=6, ts=1_757_000_500),
    ]
    records = parse_wallet_swaps("base", WALLET, tokentx)
    assert len(records) == 2
    buy = next(r for r in records if r.direction == "buy")
    sell = next(r for r in records if r.direction == "sell")
    assert buy.token_mint == TOKEN and buy.counter_mint == USDC_BASE
    assert buy.sol_amount == 100.0 and buy.token_amount == 1000.0
    assert sell.sol_amount == 150.0
    assert buy.wallet == W  # normalized lowercase


def test_weth_counter_buy():
    tokentx = [
        token_row("0xb", WETH_BASE, W, POOL, 0.5),
        token_row("0xb", TOKEN, POOL, W, 2000),
    ]
    records = parse_wallet_swaps("base", WALLET, tokentx)
    assert len(records) == 1
    assert records[0].counter_mint == WETH_BASE
    assert records[0].sol_amount == 0.5


def test_native_eth_buy_joined_from_txlist():
    tokentx = [token_row("0xn", TOKEN, POOL, W, 500)]
    txlist = [{"hash": "0xn", "from": W, "to": POOL,
               "value": str(int(0.25 * 1e18)), "isError": "0"}]
    records = parse_wallet_swaps("base", WALLET, tokentx, txlist=txlist)
    assert len(records) == 1
    assert records[0].direction == "buy"
    assert records[0].counter_mint == NATIVE
    assert records[0].sol_amount == 0.25


def test_native_eth_sell_joined_from_internal():
    tokentx = [token_row("0xs", TOKEN, W, POOL, 500)]
    internal = [{"hash": "0xs", "from": POOL, "to": W,
                 "value": str(int(0.4 * 1e18)), "isError": "0"}]
    records = parse_wallet_swaps("base", WALLET, tokentx, internal=internal)
    assert len(records) == 1
    assert records[0].direction == "sell"
    assert records[0].sol_amount == 0.4


def test_ambiguous_transactions_skipped():
    # multi-hop: two non-counter tokens in one hash
    multi = [
        token_row("0xm", TOKEN, POOL, W, 100),
        token_row("0xm", TOKEN2, W, POOL, 50),
    ]
    assert parse_wallet_swaps("base", WALLET, multi) == []
    # one-sided token receive with no counter leg and no native join
    one_sided = [token_row("0xo", TOKEN, POOL, W, 100)]
    assert parse_wallet_swaps("base", WALLET, one_sided) == []
    # two distinct counters out (route through both WETH and USDC)
    two_counters = [
        token_row("0xt", TOKEN, POOL, W, 100),
        token_row("0xt", USDC_BASE, W, POOL, 50, decimals=6),
        token_row("0xt", WETH_BASE, W, POOL, 0.01),
    ]
    assert parse_wallet_swaps("base", WALLET, two_counters) == []


def test_failed_native_tx_ignored():
    tokentx = [token_row("0xn", TOKEN, POOL, W, 500)]
    txlist = [{"hash": "0xn", "from": W, "to": POOL,
               "value": str(int(1e18)), "isError": "1"}]
    assert parse_wallet_swaps("base", WALLET, tokentx, txlist=txlist) == []


# --- mixed-counter round trips -------------------------------------------

def test_round_trips_do_not_cross_counters():
    """A buy in USDC must not match a sell into WETH — different units."""
    swaps = [
        {"token_mint": TOKEN, "direction": "buy", "token_amount": 100,
         "sol_amount": 100, "counter_mint": USDC_BASE, "block_time": 1},
        {"token_mint": TOKEN, "direction": "sell", "token_amount": 100,
         "sol_amount": 0.05, "counter_mint": WETH_BASE, "block_time": 2},
    ]
    assert extract_round_trips(swaps) == []
    swaps[1]["counter_mint"] = USDC_BASE
    swaps[1]["sol_amount"] = 150
    trips = extract_round_trips(swaps)
    assert len(trips) == 1 and abs(trips[0].return_pct - 50.0) < 1e-9


# --- provider request handling -------------------------------------------

async def test_provider_treats_no_transactions_as_empty(monkeypatch):
    provider = EtherscanProvider("fake-key")

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "0", "message": "No transactions found",
                    "result": []}

    async def fake_get(url, params=None):
        assert params["chainid"] == CHAIN_IDS["base"]
        return Resp()

    monkeypatch.setattr(provider._client, "get", fake_get)
    assert await provider._account_rows("base", "tokentx", WALLET, 100) == []
    await provider.close()


async def test_provider_invalid_key_returns_none(monkeypatch):
    provider = EtherscanProvider("bad-key")
    provider._min_interval = 0  # no throttling in tests

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "0", "message": "NOTOK",
                    "result": "Invalid API Key"}

    async def fake_get(url, params=None):
        return Resp()

    monkeypatch.setattr(provider._client, "get", fake_get)
    assert await provider._get("base", {"module": "account"}, retries=0) is None
    await provider.close()


async def test_health_check_shape(monkeypatch):
    provider = EtherscanProvider("fake-key")

    async def fake_get(chain, params, retries=3):
        return "123456789"  # balance in wei as string

    monkeypatch.setattr(provider, "_get", fake_get)
    health = await provider.health_check("ethereum")
    assert health["ok"] is True

    async def drifted(chain, params, retries=3):
        return {"unexpected": True}

    monkeypatch.setattr(provider, "_get", drifted)
    health = await provider.health_check("ethereum")
    assert health["ok"] is False
    await provider.close()


# --- EVM scanner end to end ----------------------------------------------

class StubEvmSource:
    chain = "base"

    def __init__(self, wallet_histories, token_wallets):
        self.wallet_histories = wallet_histories
        self.token_wallets = token_wallets

    async def token_activity(self, mint):
        return [], self.token_wallets.get(mint, [])

    async def get_address_swaps(self, address, limit=100, before=None):
        return self.wallet_histories.get(address, [])


def rec(wallet, mint, direction, tokens, counter_amt, counter, ts):
    return SwapRecord("base", wallet, f"{wallet}-{mint}-{direction}-{ts}",
                      mint, direction, tokens, counter_amt, counter, ts)


async def test_evm_scanner_scores_wallets(db):
    now = time.time()
    settings = make_settings()
    reputation = WalletReputationStore(db)
    db.execute(
        "INSERT INTO opportunities (id, chain, token_address, source, status, "
        "created_at, updated_at) VALUES ('opp_evm', 'base', ?, 'test', "
        "'rejected', ?, ?)", (TOKEN, now, now))
    winner = "0xwinner"
    source = StubEvmSource(
        wallet_histories={winner: [
            rec(winner, TOKEN, "buy", 100, 100, USDC_BASE, now - 4000),
            rec(winner, TOKEN, "sell", 100, 200, USDC_BASE, now - 3000),
            rec(winner, TOKEN2, "buy", 50, 0.1, WETH_BASE, now - 2500),
            rec(winner, TOKEN2, "sell", 50, 0.15, WETH_BASE, now - 2000),
            rec(winner, TOKEN, "buy", 10, 20, USDC_BASE, now - 1500),
            rec(winner, TOKEN, "sell", 10, 25, USDC_BASE, now - 1000),
        ]},
        token_wallets={TOKEN: [winner, "0xdead1"]})
    scanner = SmartMoneyScanner(settings, db, source, reputation, chain="base")
    stats = await scanner.scan()
    assert stats["chain"] == "base"
    assert stats["wallets_scored"] == 1
    row = db.query_one("SELECT * FROM wallet_scores WHERE address = ?", (winner,))
    assert row["chain"] == "base"
    assert row["trade_count"] == 3
    assert row["win_rate"] == 1.0


def test_evm_whale_signal_stable_only(db):
    reputation = WalletReputationStore(db)
    now = time.time()

    def store(wallet, direction, amount, counter, sig):
        db.execute(
            "INSERT INTO wallet_swaps (chain, wallet, signature, token_mint, "
            "direction, token_amount, sol_amount, counter_mint, block_time, "
            "recorded_at) VALUES ('base', ?, ?, ?, ?, 1000, ?, ?, ?, ?)",
            (wallet, sig, TOKEN, direction, amount, counter, now, now))

    store("0xw1", "buy", 8000, USDC_BASE, "s1")      # whale buy (stable)
    store("0xw2", "sell", 2.0, WETH_BASE, "s2")      # WETH: unit ambiguous, ignored
    store("0xw3", "buy", 100, USDC_BASE, "s3")       # below threshold
    _, whale = reputation.token_signals("base", TOKEN,
                                        whale_usd_threshold=5000.0)
    assert whale == 100.0  # only the stable whale buy counts

    store("0xw4", "sell", 8000, USDC_BASE, "s4")
    _, whale2 = reputation.token_signals("base", TOKEN,
                                         whale_usd_threshold=5000.0)
    assert whale2 == 50.0  # balanced stable whale flow


def test_evm_scanners_wired_with_key(monkeypatch):
    from tradeos.app import build_app

    monkeypatch.setenv("TRADEOS_ETHERSCAN_API_KEY", "fake-etherscan-key")
    monkeypatch.delenv("TRADEOS_HELIUS_API_KEY", raising=False)
    app = build_app(make_settings())
    chains = sorted(s.chain for s in app.scanners)
    assert chains == ["arbitrum", "base", "bsc", "ethereum", "polygon"]
    assert app.smartmoney_scanner is None  # no solana scanner without Helius
    assert app.webhook_manager is None
    assert all(isinstance(s.swap_source, EtherscanSwapSource)
               for s in app.scanners)
    app.db.close()
