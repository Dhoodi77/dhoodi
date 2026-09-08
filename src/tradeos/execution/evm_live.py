"""EVM live executor — 0x AllowanceHolder flow, external signer.

Same discipline as the Solana engine: fail closed at every step, pending
trade row before signing, gas budget in USD checked before submission,
estimateGas as an independent simulation gate on top of the venue's own,
fills reconciled from receipt Transfer logs, unresolved trades left
pending for the reconciliation loop.

Buys spend the native coin (no allowance needed). Sells require an ERC-20
allowance to the venue's AllowanceHolder: when missing, an exact-amount
approve transaction runs first through the same sign/submit/receipt path.

Proceeds of a sell are recorded from the validated quote (audited as an
estimate): native inflows don't appear in ERC-20 logs, and pretending
otherwise would be fabrication.
"""
from __future__ import annotations

import asyncio
import logging
import time

from tradeos.config import Mode, Settings
from tradeos.db.database import Database
from tradeos.execution.instructions import TradeInstruction
from tradeos.execution.paper import ExecutionResult
from tradeos.execution.signer_client import RemoteSignerClient, SignerRefused
from tradeos.execution.venues.zerox import (
    NATIVE_SENTINEL, ZeroExVenue, validate_evm_quote)
from tradeos.portfolio.accounting import PortfolioAccounting
from tradeos.providers.chains.etherscan import CHAIN_IDS, WRAPPED_NATIVE
from tradeos.providers.chains.evm import EvmProvider
from tradeos.wallets.registry import WalletRegistry

logger = logging.getLogger(__name__)

WEI = 10**18


class EvmLiveExecutor:
    def __init__(self, settings: Settings, db: Database,
                 registry: WalletRegistry, signer: RemoteSignerClient,
                 venue: ZeroExVenue, providers: dict[str, EvmProvider],
                 market, accounting: PortfolioAccounting):
        self.settings = settings
        self.db = db
        self.registry = registry
        self.signer = signer
        self.venue = venue
        self.providers = providers
        self.market = market
        self.accounting = accounting

    # --- readiness -----------------------------------------------------
    def readiness_problems(self, chain: str) -> list[str]:
        problems: list[str] = []
        if chain not in CHAIN_IDS:
            problems.append(f"unsupported EVM chain {chain!r}")
            return problems
        if self.settings.mode != Mode.LIVE:
            problems.append(f"mode is {self.settings.mode.value}, not live")
        if self.settings.live_trading_confirm != "I_UNDERSTAND_THE_RISKS":
            problems.append("TRADEOS_LIVE_TRADING_CONFIRM not set")
        if self.registry.trading_wallet(chain) is None:
            problems.append(f"no active trading wallet registered for {chain}")
        if not self.signer.configured:
            problems.append("external signer not configured")
        if not self.venue.configured:
            problems.append("0x venue not configured (TRADEOS_ZEROX_API_KEY)")
        provider = self.providers.get(chain)
        if provider is None or not getattr(provider, "rpc_url", None):
            problems.append(f"no RPC configured (TRADEOS_RPC_{chain.upper()})")
        if self.market is None:
            problems.append("no market data provider for USD conversion")
        return problems

    async def _native_price_usd(self, chain: str) -> float:
        wrapped = next(iter(WRAPPED_NATIVE.get(chain, [])), None)
        if wrapped is None:
            return 0.0
        pairs = await self.market.get_token_pairs(chain, wrapped)
        if not pairs:
            return 0.0
        return max(pairs, key=lambda p: p.liquidity_usd).price_usd

    def _fail(self, instr: TradeInstruction, error: str,
              trade_id: int | None = None) -> ExecutionResult:
        if trade_id is not None:
            self.db.execute("UPDATE trades SET status = 'failed', error = ? "
                            "WHERE id = ?", (error, trade_id))
        else:
            self.db.execute(
                "INSERT INTO trades (opportunity_id, position_id, chain, "
                "token_address, symbol, side, mode, requested_usd, status, error, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, 'live', ?, 'failed', ?, ?)",
                (instr.opportunity_id, instr.position_id, instr.chain,
                 instr.token_address, instr.symbol, instr.side, instr.amount_usd,
                 error, time.time()))
        self.db.audit("execution_live_evm", "live_refused", instr.opportunity_id,
                      {"error": error, "chain": instr.chain, "side": instr.side})
        return ExecutionResult(False, trade_id=trade_id, error=error)

    # --- transaction plumbing -------------------------------------------
    async def _sign_and_submit(self, provider: EvmProvider, chain: str,
                               wallet: dict, to: str, data: str, value: int,
                               gas_limit: int, fees: dict,
                               intent: dict) -> str:
        """Build, sign, submit. Returns tx hash; raises on refusal/failure."""
        nonce = await provider.get_nonce(wallet["address"])
        if nonce is None:
            raise RuntimeError("nonce unavailable")
        tx = {"chainId": CHAIN_IDS[chain], "nonce": nonce, "to": to,
              "value": value, "data": data, "gas": gas_limit,
              "maxFeePerGas": fees["max_fee_per_gas"],
              "maxPriorityFeePerGas": fees["max_priority_fee_per_gas"]}
        raw = await self.signer.sign_evm(tx, chain, wallet["address"], intent)
        tx_hash = await provider.send_raw_transaction(raw)
        if tx_hash is None:
            raise RuntimeError("submit response lost")
        return tx_hash

    async def _await_receipt(self, provider: EvmProvider,
                             tx_hash: str) -> dict | None:
        deadline = time.time() + self.settings.live_confirm_timeout_s
        while time.time() < deadline:
            receipt = await provider.get_receipt(tx_hash)
            if receipt is not None:
                return receipt
            await asyncio.sleep(2)
        return None

    async def _ensure_allowance(self, provider: EvmProvider, chain: str,
                                wallet: dict, token: str, spender: str,
                                amount_raw: int, fees: dict,
                                instr: TradeInstruction) -> str | None:
        """Exact-amount approval when needed. Returns an error string or None."""
        allowance = await provider.get_allowance(token, wallet["address"], spender)
        if allowance is None:
            return "allowance check failed"
        if allowance >= amount_raw:
            return None
        data = provider.approve_calldata(spender, amount_raw)
        gas = await provider.estimate_gas({
            "from": wallet["address"], "to": token, "data": data})
        if gas is None:
            return "approve simulation failed"
        try:
            tx_hash = await self._sign_and_submit(
                provider, chain, wallet, token, data, 0,
                int(gas * self.settings.live_gas_limit_multiplier), fees,
                {"kind": "approve", "token": token, "spender": spender,
                 "amount_raw": amount_raw,
                 "opportunity_id": instr.opportunity_id})
        except (SignerRefused, RuntimeError) as exc:
            return f"approve failed: {exc}"
        receipt = await self._await_receipt(provider, tx_hash)
        if receipt is None:
            return "approve not confirmed in time"
        if not receipt["ok"]:
            return "approve transaction reverted"
        self.db.audit("execution_live_evm", "allowance_approved",
                      instr.opportunity_id,
                      {"token": token, "spender": spender, "tx": tx_hash})
        return None

    # --- execution -----------------------------------------------------
    async def execute(self, instr: TradeInstruction,
                      market_price_usd: float) -> ExecutionResult:
        problems = self.readiness_problems(instr.chain)
        if problems:
            return self._fail(instr, "live prerequisites missing: "
                              + "; ".join(problems))
        chain = instr.chain
        provider = self.providers[chain]
        wallet = self.registry.trading_wallet(chain, instr.wallet_id)
        if wallet is None:
            return self._fail(instr, "no eligible trading wallet")
        if market_price_usd <= 0:
            return self._fail(instr, "no market price for token")

        native_price = await self._native_price_usd(chain)
        if native_price <= 0:
            return self._fail(instr, "native/USD price unavailable")
        decimals = await provider.get_token_decimals(instr.token_address)
        if decimals is None:
            return self._fail(instr, "token decimals unavailable")
        fees = await provider.get_fees()
        if fees is None:
            return self._fail(instr, "fee data unavailable")

        position = None
        if instr.side == "buy":
            sell_token = NATIVE_SENTINEL
            buy_token = instr.token_address
            sell_amount = int(instr.amount_usd / native_price * WEI)
        else:
            if instr.position_id is None:
                return self._fail(instr, "sell requires position_id")
            position = self.db.query_one(
                "SELECT * FROM positions WHERE id = ? AND status = 'open' "
                "AND mode = 'live'", (instr.position_id,))
            if position is None:
                return self._fail(instr, f"no open live position {instr.position_id}")
            sell_token = instr.token_address
            buy_token = NATIVE_SENTINEL
            sell_amount = int(position["quantity"] * (10 ** decimals))
        if sell_amount <= 0:
            return self._fail(instr, "computed raw amount is zero")

        slippage_bps = int(instr.max_slippage_pct * 100)
        quote = await self.venue.quote(chain, sell_token, buy_token,
                                       sell_amount, wallet["address"],
                                       slippage_bps)
        if quote is None:
            return self._fail(instr, "no quote from venue")
        check = validate_evm_quote(quote, instr.max_slippage_pct)
        if not check.ok:
            return self._fail(instr, "quote rejected: " + "; ".join(check.reasons))

        # Price sanity against the market price the pipeline supplied — the
        # venue's own numbers are never the only check.
        if instr.side == "buy":
            expected_tokens = instr.amount_usd / market_price_usd
            actual_tokens = check.buy_amount / (10 ** decimals)
        else:
            expected_usd = position["quantity"] * market_price_usd
            actual_tokens = (check.buy_amount / WEI * native_price) \
                / market_price_usd if market_price_usd else 0
            expected_tokens = expected_usd / market_price_usd
        if expected_tokens <= 0:
            return self._fail(instr, "expected fill amount is zero")
        deviation_pct = (1 - actual_tokens / expected_tokens) * 100
        allowed_deviation = instr.max_slippage_pct \
            + self.settings.live_max_price_impact_pct
        if deviation_pct > allowed_deviation:
            return self._fail(
                instr, f"fill deviates {deviation_pct:.2f}% from market price "
                f"(allowed {allowed_deviation:.2f}%)")

        # Allowance (sells only; buys spend native).
        if instr.side == "sell" and check.needs_allowance:
            if not check.allowance_spender:
                return self._fail(instr, "venue reported allowance issue "
                                         "without a spender")
            error = await self._ensure_allowance(
                provider, chain, wallet, sell_token, check.allowance_spender,
                sell_amount, fees, instr)
            if error:
                return self._fail(instr, error)

        # Independent simulation via estimateGas.
        tx_data = quote["transaction"]
        value = int(tx_data.get("value") or 0)
        gas_est = await provider.estimate_gas({
            "from": wallet["address"], "to": tx_data["to"],
            "data": tx_data["data"], "value": hex(value) if value else None})
        if gas_est is None:
            return self._fail(instr, "swap simulation (estimateGas) failed")
        gas_limit = int(gas_est * self.settings.live_gas_limit_multiplier)

        # Gas budget in USD must fit the instruction's cap.
        gas_usd = gas_limit * fees["max_fee_per_gas"] / WEI * native_price
        if gas_usd > instr.max_gas_usd:
            return self._fail(
                instr, f"gas budget ${gas_usd:.2f} exceeds instruction "
                f"max_gas ${instr.max_gas_usd:.2f}")

        # Estimated fill from the validated quote.
        if instr.side == "buy":
            est_quantity = check.buy_amount / (10 ** decimals)
            est_filled_usd = instr.amount_usd
        else:
            est_quantity = position["quantity"]
            est_filled_usd = check.buy_amount / WEI * native_price
        est_price = est_filled_usd / est_quantity if est_quantity else 0

        now = time.time()
        trade_id = self.db.execute(
            "INSERT INTO trades (opportunity_id, position_id, wallet_id, chain, "
            "token_address, symbol, side, mode, requested_usd, price_usd, quantity, "
            "slippage_pct, gas_usd, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'live', ?, ?, ?, ?, ?, 'pending', ?)",
            (instr.opportunity_id, instr.position_id, wallet["id"], chain,
             instr.token_address, instr.symbol, instr.side, instr.amount_usd,
             est_price, est_quantity, instr.max_slippage_pct, gas_usd, now))
        self.db.audit("execution_live_evm", "trade_submitting",
                      instr.opportunity_id,
                      {"trade_id": trade_id, "chain": chain, "side": instr.side,
                       "sell_amount_raw": sell_amount,
                       "quote_buy_amount": check.buy_amount,
                       "deviation_pct": round(deviation_pct, 3),
                       "gas_usd": round(gas_usd, 4)})

        try:
            tx_hash = await self._sign_and_submit(
                provider, chain, wallet, tx_data["to"], tx_data["data"], value,
                gas_limit, fees,
                {"kind": "swap", "opportunity_id": instr.opportunity_id,
                 "side": instr.side, "amount_usd": instr.amount_usd,
                 "token": instr.token_address})
        except SignerRefused as exc:
            return self._fail(instr, f"signer refused: {exc}", trade_id)
        except RuntimeError as exc:
            if "submit response lost" in str(exc):
                self.db.execute(
                    "UPDATE trades SET error = 'submit response lost: "
                    "reconciling' WHERE id = ?", (trade_id,))
                self.db.alert("critical", "EVM submit response lost",
                              f"trade {trade_id}: signed tx submitted but RPC "
                              "response lost; reconciliation running")
                return ExecutionResult(False, trade_id=trade_id,
                                       error="submit response lost; reconciling")
            return self._fail(instr, str(exc), trade_id)

        self.db.execute("UPDATE trades SET tx_hash = ? WHERE id = ?",
                        (tx_hash, trade_id))
        receipt = await self._await_receipt(provider, tx_hash)
        if receipt is None:
            self.db.alert("high", "EVM trade unconfirmed",
                          f"trade {trade_id} ({tx_hash[:16]}…) not mined within "
                          f"{self.settings.live_confirm_timeout_s}s; monitor "
                          "will reconcile")
            return ExecutionResult(False, trade_id=trade_id,
                                   error="confirmation timeout; reconciling")
        if not receipt["ok"]:
            self.db.execute(
                "UPDATE trades SET status = 'failed', error = 'transaction "
                "reverted on-chain' WHERE id = ?", (trade_id,))
            return ExecutionResult(False, trade_id=trade_id,
                                   error="transaction reverted on-chain")
        return await self._finalize(trade_id, instr.opportunity_id, tx_hash,
                                    wallet, receipt, native_price, decimals)

    # --- fill finalization ---------------------------------------------
    async def _finalize(self, trade_id: int, opportunity_id: str | None,
                        tx_hash: str, wallet: dict, receipt: dict,
                        native_price: float, decimals: int) -> ExecutionResult:
        trade = self.db.query_one("SELECT * FROM trades WHERE id = ?", (trade_id,))
        provider = self.providers[trade["chain"]]
        raw_delta = provider.token_delta_from_logs(
            receipt["logs"], trade["token_address"], wallet["address"])
        gas_usd = receipt["gas_used"] * receipt["effective_gas_price"] / WEI \
            * native_price
        now = time.time()

        if trade["side"] == "buy":
            fill_source = "receipt_logs" if raw_delta > 0 else "quote_estimate"
            quantity = raw_delta / (10 ** decimals) if raw_delta > 0 \
                else trade["quantity"]
            cost_usd = trade["requested_usd"]
            price = cost_usd / quantity if quantity else trade["price_usd"]
            position_id = self.db.execute(
                "INSERT INTO positions (opportunity_id, wallet_id, chain, "
                "token_address, symbol, mode, status, entry_price_usd, quantity, "
                "cost_usd, stop_loss_pct, take_profit_pct, max_hold_hours, "
                "last_price_usd, opened_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'live', 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (opportunity_id, wallet["id"], trade["chain"],
                 trade["token_address"], trade["symbol"], price, quantity,
                 cost_usd, self.settings.exit_stop_loss_pct,
                 self.settings.exit_take_profit_pct,
                 self.settings.exit_max_hold_hours, price, now, now))
            self.db.execute(
                "UPDATE trades SET status = 'filled', filled_usd = ?, "
                "price_usd = ?, quantity = ?, position_id = ?, gas_usd = ?, "
                "filled_at = ? WHERE id = ?",
                (cost_usd, price, quantity, position_id, gas_usd, now, trade_id))
            self.accounting.record_cash("buy", -cost_usd, trade_id,
                                        f"live buy {trade['symbol']}")
            result_position = position_id
        else:
            fill_source = "quote_estimate"  # native proceeds are not in ERC-20 logs
            position = self.db.query_one("SELECT * FROM positions WHERE id = ?",
                                         (trade["position_id"],))
            proceeds_usd = (trade["price_usd"] or 0) * (trade["quantity"] or 0)
            realized = proceeds_usd - position["cost_usd"]
            fill_price = proceeds_usd / position["quantity"] \
                if position["quantity"] else 0
            self.db.execute(
                "UPDATE positions SET status = 'closed', exit_price_usd = ?, "
                "exit_reason = 'live_sell', realized_pnl_usd = ?, "
                "last_price_usd = ?, closed_at = ?, updated_at = ? WHERE id = ?",
                (fill_price, realized, fill_price, now, now, position["id"]))
            self.db.execute(
                "UPDATE trades SET status = 'filled', filled_usd = ?, "
                "price_usd = ?, gas_usd = ?, filled_at = ? WHERE id = ?",
                (proceeds_usd, fill_price, gas_usd, now, trade_id))
            self.accounting.record_cash("sell", proceeds_usd, trade_id,
                                        f"live sell {trade['symbol']}")
            result_position = position["id"]

        self.accounting.record_cash("fee", -gas_usd, trade_id, "gas")
        self.db.audit("execution_live_evm", "trade_filled", opportunity_id, {
            "trade_id": trade_id, "tx_hash": tx_hash,
            "fill_source": fill_source, "gas_usd": round(gas_usd, 4)})
        self.db.alert("high",
                      f"LIVE {trade['side']} filled: {trade['symbol']} "
                      f"({trade['chain']})",
                      f"tx {tx_hash[:20]}… (fill: {fill_source})")
        fill = self.db.query_one("SELECT price_usd FROM trades WHERE id = ?",
                                 (trade_id,))
        return ExecutionResult(True, trade_id=trade_id,
                               position_id=result_position,
                               fill_price=fill["price_usd"])

    # --- reconciliation -------------------------------------------------
    async def reconcile_pending(self) -> int:
        resolved = 0
        pending = self.db.query(
            "SELECT * FROM trades WHERE mode = 'live' AND status = 'pending' "
            "AND chain != 'solana'")
        for trade in pending:
            provider = self.providers.get(trade["chain"])
            if provider is None:
                continue
            if not trade["tx_hash"]:
                if time.time() - trade["created_at"] > 300:
                    self.db.execute(
                        "UPDATE trades SET status = 'failed', error = "
                        "'no tx hash recorded: verify wallet manually' "
                        "WHERE id = ?", (trade["id"],))
                    self.db.alert("critical", "Unresolvable EVM trade",
                                  f"trade {trade['id']}: no tx hash; check the "
                                  "wallet's on-chain history manually")
                    resolved += 1
                continue
            receipt = await provider.get_receipt(trade["tx_hash"])
            if receipt is None:
                if time.time() - trade["created_at"] > 1800:
                    self.db.execute(
                        "UPDATE trades SET status = 'failed', error = "
                        "'never mined (likely dropped)' WHERE id = ?",
                        (trade["id"],))
                    self.db.alert("critical", "EVM trade dropped",
                                  f"trade {trade['id']}: not mined after 30m; "
                                  "verify the wallet nonce state manually")
                    resolved += 1
                continue
            if not receipt["ok"]:
                self.db.execute(
                    "UPDATE trades SET status = 'failed', error = "
                    "'transaction reverted on-chain' WHERE id = ?", (trade["id"],))
                resolved += 1
                continue
            wallet = self.registry.get(trade["wallet_id"]) if trade["wallet_id"] \
                else self.registry.trading_wallet(trade["chain"])
            native_price = await self._native_price_usd(trade["chain"])
            decimals = await provider.get_token_decimals(trade["token_address"])
            if wallet and native_price > 0 and decimals is not None:
                await self._finalize(trade["id"], trade["opportunity_id"],
                                     trade["tx_hash"], wallet, receipt,
                                     native_price, decimals)
                resolved += 1
        return resolved
