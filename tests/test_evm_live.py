"""EVM live execution: 0x quote validation, executor flow, allowances,
receipt fills, reconciliation. All offline against shape-accurate stubs.
"""
import time

from tradeos.config import Mode
from tradeos.execution.evm_live import WEI, EvmLiveExecutor
from tradeos.execution.instructions import TradeInstruction
from tradeos.execution.live import LiveExecutionEngine
from tradeos.execution.signer_client import SignerRefused
from tradeos.execution.venues.zerox import NATIVE_SENTINEL, validate_evm_quote
from tradeos.portfolio.accounting import PortfolioAccounting
from tradeos.providers.base import PairData
from tradeos.providers.chains.evm import TRANSFER_TOPIC, EvmProvider
from tradeos.wallets.registry import WalletRegistry

from .conftest import make_settings

TOKEN = "0x1111111111111111111111111111111111111111"
WALLET_ADDR = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SPENDER = "0x2222222222222222222222222222222222222222"
ROUTER = "0x3333333333333333333333333333333333333333"


# --- quote validation -----------------------------------------------------

def good_evm_quote(buy_amount=5 * 10**18, min_ratio=0.99, allowance=None):
    return {"liquidityAvailable": True,
            "buyAmount": str(buy_amount),
            "minBuyAmount": str(int(buy_amount * min_ratio)),
            "sellAmount": "10000000000000000",
            "transaction": {"to": ROUTER, "data": "0xdeadbeef", "value": "0",
                            "gas": "200000"},
            "issues": {"allowance": allowance, "balance": None,
                       "simulationIncomplete": False}}


def test_evm_quote_validation():
    assert validate_evm_quote(good_evm_quote(), 1.0).ok
    assert not validate_evm_quote({"liquidityAvailable": False}, 1.0).ok
    assert not validate_evm_quote(good_evm_quote(min_ratio=0.90), 1.0).ok  # 10% slip
    q = good_evm_quote()
    q["issues"]["simulationIncomplete"] = True
    assert not validate_evm_quote(q, 1.0).ok
    q = good_evm_quote()
    q["transaction"] = {}
    assert not validate_evm_quote(q, 1.0).ok
    check = validate_evm_quote(
        good_evm_quote(allowance={"actual": "0", "spender": SPENDER}), 1.0)
    assert check.ok and check.needs_allowance
    assert check.allowance_spender == SPENDER


def test_transfer_log_delta():
    wallet_topic = "0x" + WALLET_ADDR.replace("0x", "").rjust(64, "0")
    other_topic = "0x" + "9" * 64
    logs = [
        {"address": TOKEN, "topics": [TRANSFER_TOPIC, other_topic, wallet_topic],
         "data": hex(500)},
        {"address": TOKEN, "topics": [TRANSFER_TOPIC, wallet_topic, other_topic],
         "data": hex(100)},
        {"address": "0xother", "topics": [TRANSFER_TOPIC, other_topic,
                                          wallet_topic], "data": hex(999)},
    ]
    assert EvmProvider.token_delta_from_logs(logs, TOKEN, WALLET_ADDR) == 400


# --- stubs ----------------------------------------------------------------

class StubZeroX:
    configured = True

    def __init__(self, quote):
        self._quote = quote
        self.calls = []

    async def quote(self, chain, sell_token, buy_token, sell_amount, taker,
                    slippage_bps):
        self.calls.append({"sell_token": sell_token, "buy_token": buy_token,
                           "sell_amount": sell_amount})
        return self._quote

    async def health_check(self, chain="base"):
        return {"provider": "zerox", "ok": True}


class StubEvmRpc:
    rpc_url = "https://stub.evm"

    def __init__(self, decimals=18, allowance=0, receipt_ok=True,
                 receipt_logs=None, estimate=150_000, receipt_delay=0):
        self.decimals = decimals
        self.allowance = allowance
        self.receipt_ok = receipt_ok
        self.receipt_logs = receipt_logs if receipt_logs is not None else []
        self.estimate = estimate
        self.receipt_delay = receipt_delay  # calls before receipt appears
        self._receipt_polls = 0
        self.sent_raw: list[str] = []
        self.estimate_calls: list[dict] = []
        self.nonce = 7

    async def is_available(self):
        return True

    async def get_token_decimals(self, token):
        return self.decimals

    async def get_allowance(self, token, owner, spender):
        return self.allowance

    approve_calldata = staticmethod(EvmProvider.approve_calldata)
    token_delta_from_logs = staticmethod(EvmProvider.token_delta_from_logs)

    async def get_nonce(self, address):
        return self.nonce

    async def get_fees(self):
        return {"max_fee_per_gas": 2 * 10**9, "max_priority_fee_per_gas": 10**8}

    async def estimate_gas(self, tx):
        self.estimate_calls.append(tx)
        return self.estimate

    async def send_raw_transaction(self, raw):
        self.sent_raw.append(raw)
        return "0xhash" + str(len(self.sent_raw))

    async def get_receipt(self, tx_hash):
        self._receipt_polls += 1
        if self._receipt_polls <= self.receipt_delay:
            return None
        return {"ok": self.receipt_ok, "gas_used": 120_000,
                "effective_gas_price": 10**9, "logs": self.receipt_logs}


class StubSigner:
    configured = True

    def __init__(self, refuse=False):
        self.refuse = refuse
        self.evm_calls: list[dict] = []

    async def health(self):
        return True

    async def sign_evm(self, tx, chain, wallet, intent):
        self.evm_calls.append({"tx": tx, "intent": intent})
        if self.refuse:
            raise SignerRefused("policy: value cap")
        return "0xsigned" + str(len(self.evm_calls))


class StubMarket:
    """Serves WETH price (native) and nothing else needed."""

    def __init__(self, native_price=2000.0):
        self.native_price = native_price

    async def get_token_pairs(self, chain, mint):
        if self.native_price <= 0:
            return []
        return [PairData(chain=chain, pair_address="p", token_address=mint,
                         price_usd=self.native_price, liquidity_usd=1e9)]


def buy_logs(amount_raw):
    wallet_topic = "0x" + WALLET_ADDR.replace("0x", "").rjust(64, "0")
    return [{"address": TOKEN,
             "topics": [TRANSFER_TOPIC, "0x" + "9" * 64, wallet_topic],
             "data": hex(amount_raw)}]


def make_executor(db, *, quote=None, rpc=None, signer=None, market=None,
                  **settings_overrides):
    settings = make_settings(mode=Mode.LIVE,
                             live_trading_confirm="I_UNDERSTAND_THE_RISKS",
                             **settings_overrides)
    registry = WalletRegistry(db)
    registry.register("evm-main", "base", WALLET_ADDR)
    accounting = PortfolioAccounting(db, "live")
    accounting.record_cash("deposit", 500, note="live float")
    rpc = rpc if rpc is not None else StubEvmRpc()
    executor = EvmLiveExecutor(
        settings, db, registry,
        signer if signer is not None else StubSigner(),
        StubZeroX(quote if quote is not None else good_evm_quote()),
        {"base": rpc}, market if market is not None else StubMarket(),
        accounting)
    return executor, rpc


def buy_instr(**kw):
    d = dict(opportunity_id="opp_evm", chain="base", token_address=TOKEN,
             symbol="TOK", side="buy", amount_usd=20.0, max_slippage_pct=1.0,
             max_gas_usd=1.0)
    d.update(kw)
    return TradeInstruction(**d)


# --- readiness ------------------------------------------------------------

async def test_evm_readiness_gating(db):
    settings = make_settings(mode=Mode.PAPER)
    executor = EvmLiveExecutor(settings, db, WalletRegistry(db), StubSigner(),
                               StubZeroX(good_evm_quote()), {}, None,
                               PortfolioAccounting(db, "live"))
    problems = " ".join(executor.readiness_problems("base"))
    for expected in ("mode is paper", "CONFIRM", "wallet", "RPC", "market"):
        assert expected in problems
    assert executor.readiness_problems("tron") == ["unsupported EVM chain 'tron'"]


async def test_engine_dispatches_evm_chain(db):
    executor, rpc = make_executor(db, quote=good_evm_quote(
        buy_amount=int(0.01 * WEI / 4 * 10**6)))  # irrelevant token amount
    engine = LiveExecutionEngine(executor.settings, db, evm=executor)
    # dispatch reaches the evm executor's own logic (fails on price check,
    # not on "not configured")
    result = await engine.execute(buy_instr(), 4.0)
    assert "not configured" not in (result.error or "")


# --- buy flow -------------------------------------------------------------

async def test_evm_buy_full_flow_with_receipt_fill(db):
    # $20 at $4/token -> expect ~5 tokens; quote delivers 4.98, receipt 4.97
    quote = good_evm_quote(buy_amount=int(4.98 * WEI))
    rpc = StubEvmRpc(receipt_logs=buy_logs(int(4.97 * WEI)))
    executor, _ = make_executor(db, quote=quote, rpc=rpc)
    result = await executor.execute(buy_instr(), market_price_usd=4.0)
    assert result.ok, result.error

    trade = db.query_one("SELECT * FROM trades WHERE id = ?", (result.trade_id,))
    assert trade["status"] == "filled"
    assert trade["tx_hash"] == "0xhash1"
    pos = db.query_one("SELECT * FROM positions WHERE id = ?",
                       (result.position_id,))
    assert abs(pos["quantity"] - 4.97) < 1e-9  # from receipt logs, not quote
    assert pos["chain"] == "base" and pos["mode"] == "live"
    # buys spend native: no approval tx, exactly one submission
    assert len(rpc.sent_raw) == 1
    # gas fee booked in the ledger: 120000 * 1 gwei * $2000 = $0.24
    fee = db.query_one("SELECT * FROM portfolio_ledger WHERE kind = 'fee'")
    assert abs(fee["amount_usd"] + 0.24) < 1e-6
    audit = db.query("SELECT * FROM audit_log WHERE action = 'trade_filled'")
    assert "receipt_logs" in audit[0]["detail_json"]


async def test_price_deviation_refused(db):
    # market says $4/token -> expect 5 tokens; quote only delivers 4.5 (-10%)
    quote = good_evm_quote(buy_amount=int(4.5 * WEI))
    executor, rpc = make_executor(db, quote=quote)
    result = await executor.execute(buy_instr(), 4.0)
    assert not result.ok
    assert "deviates" in result.error
    assert rpc.sent_raw == []


async def test_estimate_gas_failure_stops_before_signing(db):
    signer = StubSigner()
    rpc = StubEvmRpc(estimate=None)
    executor, _ = make_executor(db, quote=good_evm_quote(
        buy_amount=int(4.99 * WEI)), rpc=rpc, signer=signer)
    result = await executor.execute(buy_instr(), 4.0)
    assert not result.ok
    assert "estimateGas" in result.error
    assert signer.evm_calls == []
    assert rpc.sent_raw == []


async def test_gas_budget_refused(db):
    executor, rpc = make_executor(db, quote=good_evm_quote(
        buy_amount=int(4.99 * WEI)))
    # 150k*1.2 gas * 2 gwei * $2000 = $0.72 > $0.10
    result = await executor.execute(buy_instr(max_gas_usd=0.10), 4.0)
    assert not result.ok
    assert "gas budget" in result.error
    assert rpc.sent_raw == []


async def test_signer_refusal_marks_failed(db):
    executor, rpc = make_executor(db, quote=good_evm_quote(
        buy_amount=int(4.99 * WEI)), signer=StubSigner(refuse=True))
    result = await executor.execute(buy_instr(), 4.0)
    assert not result.ok
    assert "signer refused" in result.error
    assert rpc.sent_raw == []
    trade = db.query_one("SELECT * FROM trades WHERE mode = 'live'")
    assert trade["status"] == "failed"


async def test_reverted_receipt_no_position(db):
    rpc = StubEvmRpc(receipt_ok=False)
    executor, _ = make_executor(db, quote=good_evm_quote(
        buy_amount=int(4.99 * WEI)), rpc=rpc)
    result = await executor.execute(buy_instr(), 4.0)
    assert not result.ok
    assert "reverted" in result.error
    assert db.query("SELECT * FROM positions") == []


# --- sell flow with allowance ---------------------------------------------

async def open_live_position(db, executor):
    now = time.time()
    return db.execute(
        "INSERT INTO positions (opportunity_id, wallet_id, chain, token_address, "
        "symbol, mode, status, entry_price_usd, quantity, cost_usd, opened_at, "
        "updated_at) VALUES ('opp_evm', 1, 'base', ?, 'TOK', 'live', 'open', "
        "4.0, 5.0, 20.0, ?, ?)", (TOKEN, now, now))


async def test_evm_sell_runs_approval_first(db):
    # proceeds: 0.015 native * $2000 = $30 for the 5-token position
    quote = good_evm_quote(buy_amount=int(0.015 * WEI),
                           allowance={"actual": "0", "spender": SPENDER})
    rpc = StubEvmRpc(allowance=0)
    signer = StubSigner()
    executor, _ = make_executor(db, quote=quote, rpc=rpc, signer=signer)
    pos_id = await open_live_position(db, executor)

    result = await executor.execute(
        buy_instr(side="sell", position_id=pos_id), 4.0)
    assert result.ok, result.error
    # two signed transactions: approve then swap
    assert len(signer.evm_calls) == 2
    approve_tx = signer.evm_calls[0]["tx"]
    assert approve_tx["to"] == TOKEN                    # approve on the token
    assert approve_tx["data"].startswith("0x095ea7b3")  # approve() selector
    assert SPENDER.replace("0x", "") in approve_tx["data"]
    assert signer.evm_calls[1]["tx"]["to"] == ROUTER    # then the swap
    pos = db.query_one("SELECT * FROM positions WHERE id = ?", (pos_id,))
    assert pos["status"] == "closed"
    assert abs(pos["realized_pnl_usd"] - 10.0) < 1e-6   # $30 - $20


async def test_evm_sell_skips_approval_when_sufficient(db):
    quote = good_evm_quote(buy_amount=int(0.015 * WEI),
                           allowance={"actual": "0", "spender": SPENDER})
    rpc = StubEvmRpc(allowance=10**30)  # already approved plenty
    signer = StubSigner()
    executor, _ = make_executor(db, quote=quote, rpc=rpc, signer=signer)
    pos_id = await open_live_position(db, executor)
    result = await executor.execute(
        buy_instr(side="sell", position_id=pos_id), 4.0)
    assert result.ok
    assert len(signer.evm_calls) == 1  # swap only


# --- reconciliation -------------------------------------------------------

async def test_evm_timeout_then_reconcile(db):
    rpc = StubEvmRpc(receipt_delay=10**6,
                     receipt_logs=buy_logs(int(4.97 * WEI)))
    executor, _ = make_executor(db, quote=good_evm_quote(
        buy_amount=int(4.98 * WEI)), rpc=rpc, live_confirm_timeout_s=0.1)
    result = await executor.execute(buy_instr(), 4.0)
    assert not result.ok
    assert "reconciling" in result.error
    trade = db.query_one("SELECT * FROM trades WHERE id = ?", (result.trade_id,))
    assert trade["status"] == "pending"
    assert db.query("SELECT * FROM positions") == []

    rpc.receipt_delay = 0  # receipt now available
    resolved = await executor.reconcile_pending()
    assert resolved == 1
    trade = db.query_one("SELECT * FROM trades WHERE id = ?", (result.trade_id,))
    assert trade["status"] == "filled"
    assert len(db.query("SELECT * FROM positions WHERE status = 'open'")) == 1
