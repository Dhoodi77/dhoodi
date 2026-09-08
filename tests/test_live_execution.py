"""Stage 7: wallet registry, quote validation, live engine, reconciliation.

Everything offline: venue, signer, and RPC are stubs that mimic the real
response shapes. The point is proving the deterministic control flow —
what gets refused, what order things happen in, and that nothing is ever
fabricated.
"""
import time

import pytest

from tradeos.config import Mode
from tradeos.execution.instructions import TradeInstruction
from tradeos.execution.live import LiveExecutionEngine
from tradeos.execution.signer_client import SignerRefused
from tradeos.execution.venues.jupiter import (
    LAMPORTS_PER_SOL, SOL_MINT, validate_quote)
from tradeos.portfolio.accounting import PortfolioAccounting
from tradeos.providers.base import PairData
from tradeos.wallets.registry import WalletRegistry

from .conftest import make_settings

TOKEN = "TokenMint11111111111111111111111111111111111"
WALLET_ADDR = "TradingWa11et111111111111111111111111111111"


# --- wallet registry ------------------------------------------------------

def test_registry_register_and_lookup(db):
    registry = WalletRegistry(db)
    wid = registry.register("main", "solana", WALLET_ADDR, kind="trading")
    assert registry.trading_wallet("solana")["id"] == wid
    assert registry.trading_wallet("solana", wallet_id=wid)["address"] == WALLET_ADDR


def test_registry_treasury_never_eligible_for_trading(db):
    registry = WalletRegistry(db)
    wid = registry.register("vault", "solana", WALLET_ADDR, kind="treasury")
    assert registry.trading_wallet("solana") is None
    assert registry.trading_wallet("solana", wallet_id=wid) is None


def test_registry_inactive_wallet_not_eligible(db):
    registry = WalletRegistry(db)
    wid = registry.register("main", "solana", WALLET_ADDR)
    registry.set_active(wid, False)
    assert registry.trading_wallet("solana") is None


def test_registry_wrong_chain_not_eligible(db):
    registry = WalletRegistry(db)
    wid = registry.register("evm", "base", "0xabc")
    assert registry.trading_wallet("solana", wallet_id=wid) is None


def test_registry_rejects_bad_input(db):
    registry = WalletRegistry(db)
    with pytest.raises(ValueError):
        registry.register("x", "solana", WALLET_ADDR, kind="hot")
    with pytest.raises(ValueError):
        registry.register("", "solana", WALLET_ADDR)


# --- quote validation -----------------------------------------------------

def good_quote(in_amount=100_000_000, out_amount=5_000_000, impact="0.004"):
    return {"inputMint": SOL_MINT, "outputMint": TOKEN,
            "inAmount": str(in_amount), "outAmount": str(out_amount),
            "otherAmountThreshold": str(int(out_amount * 0.99)),
            "slippageBps": 100, "priceImpactPct": impact}


def test_quote_validation_accepts_good_quote():
    check = validate_quote(good_quote(), SOL_MINT, TOKEN, 100, 2.0)
    assert check.ok
    assert check.out_amount == 5_000_000


def test_quote_validation_rejects_bad_quotes():
    assert not validate_quote(good_quote(), TOKEN, SOL_MINT, 100, 2.0).ok  # mints
    assert not validate_quote(good_quote(impact="0.05"), SOL_MINT, TOKEN,
                              100, 2.0).ok  # 5% impact > 2%
    q = good_quote()
    q["slippageBps"] = 500
    assert not validate_quote(q, SOL_MINT, TOKEN, 100, 2.0).ok
    q = good_quote(out_amount=0)
    assert not validate_quote(q, SOL_MINT, TOKEN, 100, 2.0).ok
    assert not validate_quote({}, SOL_MINT, TOKEN, 100, 2.0).ok


# --- stubs ----------------------------------------------------------------

class StubVenue:
    def __init__(self, quote=None, tx="dHhfYmFzZTY0"):
        self._quote = quote
        self._tx = tx
        self.swap_calls = []

    async def quote(self, input_mint, output_mint, amount_raw, slippage_bps):
        if self._quote is None:
            return None
        q = dict(self._quote)
        q["inputMint"], q["outputMint"] = input_mint, output_mint
        return q

    async def build_swap_transaction(self, quote, user_public_key, max_fee):
        self.swap_calls.append(user_public_key)
        return self._tx

    async def health_check(self):
        return {"provider": "stub", "ok": True}


class StubSigner:
    configured = True

    def __init__(self, refuse=False, fail=False):
        self.refuse = refuse
        self.fail = fail
        self.sign_calls = []

    async def health(self):
        return True

    async def sign(self, tx, chain, wallet, intent):
        self.sign_calls.append(intent)
        if self.refuse:
            raise SignerRefused("policy: rate limit")
        if self.fail:
            raise RuntimeError("signer unreachable")
        return tx + "signed"


class StubSolana:
    rpc_url = "https://stub.rpc"

    def __init__(self, sim_ok=True, send_sig="SIG123", confirm="confirmed",
                 decimals=6, balances=None):
        self.sim_ok = sim_ok
        self.send_sig = send_sig
        self.confirm = confirm
        self.decimals = decimals
        self.balances = balances
        self.simulated = []
        self.sent = []

    async def is_available(self):
        return True

    async def get_token_decimals(self, mint):
        return self.decimals

    async def simulate_transaction(self, tx):
        self.simulated.append(tx)
        return {"ok": self.sim_ok,
                "error": None if self.sim_ok else "InstructionError"}

    async def send_transaction(self, tx):
        self.sent.append(tx)
        return self.send_sig

    async def get_signature_status(self, sig):
        if self.confirm is None:
            return None
        if self.confirm == "failed":
            return {"err": {"InstructionError": [0, "Custom"]}}
        return {"err": None, "confirmationStatus": self.confirm}

    async def get_transaction_balances(self, sig):
        return self.balances


class StubMarket:
    def __init__(self, sol_price=200.0):
        self.sol_price = sol_price

    async def get_token_pairs(self, chain, mint):
        if self.sol_price <= 0:
            return []
        return [PairData(chain=chain, pair_address="p", token_address=mint,
                         price_usd=self.sol_price, liquidity_usd=1e9)]


def make_engine(db, *, mode=Mode.LIVE, confirm="I_UNDERSTAND_THE_RISKS",
                venue=None, signer=None, solana=None, market=None,
                register_wallet=True, **settings_overrides):
    settings = make_settings(mode=mode, live_trading_confirm=confirm,
                             **settings_overrides)
    registry = WalletRegistry(db)
    if register_wallet:
        registry.register("main", "solana", WALLET_ADDR)
    accounting = PortfolioAccounting(db, "live")
    accounting.record_cash("deposit", 500, note="live float")
    engine = LiveExecutionEngine(
        settings, db, registry=registry,
        signer=signer if signer is not None else StubSigner(),
        venue=venue if venue is not None else StubVenue(good_quote()),
        solana=solana if solana is not None else StubSolana(),
        market=market if market is not None else StubMarket(),
        accounting=accounting)
    return engine


def buy_instr(**kw):
    d = dict(opportunity_id="opp_live", chain="solana", token_address=TOKEN,
             symbol="TOK", side="buy", amount_usd=20.0, max_slippage_pct=1.0,
             max_gas_usd=1.0)
    d.update(kw)
    return TradeInstruction(**d)


# --- readiness gating -----------------------------------------------------

async def test_readiness_lists_every_missing_prerequisite(db):
    settings = make_settings(mode=Mode.PAPER)
    engine = LiveExecutionEngine(settings, db)
    problems = engine.readiness_problems()
    joined = " ".join(problems)
    for expected in ("mode is paper", "LIVE_TRADING_CONFIRM", "trading wallet",
                     "signer", "venue", "RPC", "market data"):
        assert expected in joined


async def test_ready_engine_reports_no_problems(db):
    engine = make_engine(db)
    assert engine.readiness_problems() == []
    report = await engine.readiness_full()
    assert report["ready"] is True


async def test_evm_chain_refused_without_evm_executor(db):
    engine = make_engine(db)  # no evm executor wired
    result = await engine.execute(buy_instr(chain="base",
                                            token_address="0xToken"), 1.0)
    assert not result.ok
    assert "not configured" in result.error


async def test_treasury_wallet_refused(db):
    engine = make_engine(db, register_wallet=False)
    wid = engine.registry.register("vault", "solana", WALLET_ADDR,
                                   kind="treasury")
    result = await engine.execute(buy_instr(wallet_id=wid), 1.0)
    assert not result.ok


# --- live buy happy path --------------------------------------------------

async def test_live_buy_full_flow(db):
    venue = StubVenue(good_quote(out_amount=5_000_000))  # 5.0 tokens @ 6 dec
    signer = StubSigner()
    solana = StubSolana()
    engine = make_engine(db, venue=venue, signer=signer, solana=solana)

    result = await engine.execute(buy_instr(), market_price_usd=4.0)
    assert result.ok, result.error
    trade = db.query_one("SELECT * FROM trades WHERE id = ?", (result.trade_id,))
    assert trade["status"] == "filled"
    assert trade["tx_hash"] == "SIG123"
    assert trade["mode"] == "live"
    # order of operations: simulated before signed before sent
    assert len(solana.simulated) == 1
    assert len(signer.sign_calls) == 1
    assert solana.sent == ["dHhfYmFzZTY0signed"]
    # position opened with quote-estimated quantity (5.0 tokens for $20)
    pos = db.query_one("SELECT * FROM positions WHERE id = ?",
                       (result.position_id,))
    assert pos["status"] == "open" and pos["mode"] == "live"
    assert abs(pos["quantity"] - 5.0) < 1e-9
    assert abs(pos["cost_usd"] - 20.0) < 1e-9
    # cash debited
    acct = engine.accounting
    assert abs(acct.cash_usd() - 480.0) < 1e-9
    # fill source audited as estimate (no balance data from stub)
    audit = db.query("SELECT * FROM audit_log WHERE action = 'trade_filled'")
    assert "quote_estimate" in audit[0]["detail_json"]


async def test_live_buy_uses_onchain_fill_when_available(db):
    balances = {
        "err": None, "fee_lamports": 5000,
        "pre_token": [{"owner": WALLET_ADDR, "mint": TOKEN,
                       "uiTokenAmount": {"uiAmount": 0}}],
        "post_token": [{"owner": WALLET_ADDR, "mint": TOKEN,
                        "uiTokenAmount": {"uiAmount": 4.9}}],
        "pre_sol": [10 * LAMPORTS_PER_SOL],
        "post_sol": [int(9.9 * LAMPORTS_PER_SOL)],
        "account_keys": [WALLET_ADDR],
    }
    solana = StubSolana(balances=balances)
    engine = make_engine(db, solana=solana)
    result = await engine.execute(buy_instr(), 4.0)
    assert result.ok
    pos = db.query_one("SELECT * FROM positions WHERE id = ?",
                       (result.position_id,))
    assert abs(pos["quantity"] - 4.9) < 1e-9           # actual, not quote
    assert abs(pos["cost_usd"] - 0.1 * 200.0) < 1e-6   # actual SOL spent * $200


# --- refusal paths --------------------------------------------------------

async def test_simulation_failure_stops_before_signing(db):
    signer = StubSigner()
    solana = StubSolana(sim_ok=False)
    engine = make_engine(db, signer=signer, solana=solana)
    result = await engine.execute(buy_instr(), 4.0)
    assert not result.ok
    assert "simulation failed" in result.error
    assert signer.sign_calls == []       # never reached the signer
    assert solana.sent == []


async def test_signer_refusal_marks_trade_failed(db):
    solana = StubSolana()
    engine = make_engine(db, signer=StubSigner(refuse=True), solana=solana)
    result = await engine.execute(buy_instr(), 4.0)
    assert not result.ok
    assert "signer refused" in result.error
    assert solana.sent == []
    trade = db.query_one("SELECT * FROM trades WHERE mode = 'live'")
    assert trade["status"] == "failed"


async def test_bad_quote_refused_before_building_tx(db):
    venue = StubVenue(good_quote(impact="0.10"))  # 10% price impact
    engine = make_engine(db, venue=venue)
    result = await engine.execute(buy_instr(), 4.0)
    assert not result.ok
    assert "quote rejected" in result.error
    assert venue.swap_calls == []


async def test_onchain_failure_records_failed_no_position(db):
    engine = make_engine(db, solana=StubSolana(confirm="failed"))
    result = await engine.execute(buy_instr(), 4.0)
    assert not result.ok
    assert db.query("SELECT * FROM positions") == []
    trade = db.query_one("SELECT * FROM trades WHERE mode = 'live'")
    assert trade["status"] == "failed"


async def test_gas_cap_enforced_against_instruction(db):
    # fee cap 0.001 SOL * $200 = $0.20 > max_gas $0.10 -> refuse
    engine = make_engine(db)
    result = await engine.execute(buy_instr(max_gas_usd=0.10), 4.0)
    assert not result.ok
    assert "priority fee cap" in result.error


async def test_no_sol_price_refuses(db):
    engine = make_engine(db, market=StubMarket(sol_price=0))
    result = await engine.execute(buy_instr(), 4.0)
    assert not result.ok
    assert "price unavailable" in result.error


# --- confirmation timeout and reconciliation ------------------------------

async def test_confirmation_timeout_leaves_pending_then_reconciles(db):
    solana = StubSolana(confirm=None)  # never confirms during execute
    engine = make_engine(db, solana=solana, live_confirm_timeout_s=0.1)
    result = await engine.execute(buy_instr(), 4.0)
    assert not result.ok
    assert "reconciling" in result.error
    trade = db.query_one("SELECT * FROM trades WHERE id = ?", (result.trade_id,))
    assert trade["status"] == "pending"
    assert db.query("SELECT * FROM positions") == []  # no fabricated fill

    # chain later confirms -> reconciliation finalizes the position
    solana.confirm = "finalized"
    resolved = await engine.reconcile_pending()
    assert resolved == 1
    trade = db.query_one("SELECT * FROM trades WHERE id = ?", (result.trade_id,))
    assert trade["status"] == "filled"
    assert len(db.query("SELECT * FROM positions WHERE status = 'open'")) == 1


async def test_reconcile_drops_never_landed_trade(db):
    solana = StubSolana(confirm=None)
    engine = make_engine(db, solana=solana, live_confirm_timeout_s=0.1)
    result = await engine.execute(buy_instr(), 4.0)
    # backdate past the drop window; status lookups keep returning None
    db.execute("UPDATE trades SET created_at = ? WHERE id = ?",
               (time.time() - 700, result.trade_id))
    resolved = await engine.reconcile_pending()
    assert resolved == 1
    trade = db.query_one("SELECT * FROM trades WHERE id = ?", (result.trade_id,))
    assert trade["status"] == "failed"
    assert "never landed" in trade["error"]


# --- live sell ------------------------------------------------------------

async def test_live_sell_closes_position(db):
    engine = make_engine(db)
    buy = await engine.execute(buy_instr(), 4.0)
    assert buy.ok
    # sell quote: 5 tokens -> 0.15 SOL out (= $30 at $200/SOL)
    engine.venue = StubVenue(good_quote(
        in_amount=5_000_000, out_amount=int(0.15 * LAMPORTS_PER_SOL)))
    sell = await engine.execute(buy_instr(
        side="sell", position_id=buy.position_id), 6.0)
    assert sell.ok, sell.error
    pos = db.query_one("SELECT * FROM positions WHERE id = ?",
                       (buy.position_id,))
    assert pos["status"] == "closed"
    assert abs(pos["realized_pnl_usd"] - 10.0) < 1e-6  # $30 - $20
