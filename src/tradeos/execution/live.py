"""Live execution engine — Solana via Jupiter, external signer.

Fails closed at every step. The full order of operations for a live trade:

  readiness (config, wallet registry, signer, venue, RPC)
  -> deterministic sizing and mint resolution
  -> Jupiter quote  -> deterministic quote validation (slippage, price
     impact, mints, amounts)
  -> priority-fee cap check against the instruction's max_gas_usd
  -> unsigned transaction built
  -> RPC simulation (must succeed; an unreachable RPC is a failure)
  -> trade row written as 'pending' BEFORE signing, so a crash after this
     point is visible and reconciled, never silently lost
  -> external signer (its own policy may refuse)
  -> submission -> confirmation polling
  -> fill recorded from on-chain balance deltas when retrievable,
     otherwise from the validated quote (audited as an estimate)

EVM live execution is NOT implemented and refuses explicitly.

This engine never sees a private key. It is only reachable through the
ExecutionGateway, which has already applied the kill switch and the full
risk policy before this code runs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from tradeos.config import Mode, Settings
from tradeos.db.database import Database
from tradeos.execution.instructions import TradeInstruction
from tradeos.execution.paper import ExecutionResult
from tradeos.execution.signer_client import RemoteSignerClient, SignerRefused
from tradeos.execution.venues.jupiter import (
    LAMPORTS_PER_SOL, SOL_MINT, JupiterVenue, validate_quote)
from tradeos.portfolio.accounting import PortfolioAccounting
from tradeos.providers.chains.solana import SolanaProvider
from tradeos.wallets.registry import WalletRegistry

logger = logging.getLogger(__name__)


class LiveExecutionEngine:
    mode = "live"

    def __init__(self, settings: Settings, db: Database,
                 registry: WalletRegistry | None = None,
                 signer: RemoteSignerClient | None = None,
                 venue: JupiterVenue | None = None,
                 solana: SolanaProvider | None = None,
                 market=None,
                 accounting: PortfolioAccounting | None = None,
                 evm=None):
        self.settings = settings
        self.db = db
        self.registry = registry
        self.signer = signer
        self.venue = venue
        self.solana = solana
        self.market = market
        self.accounting = accounting
        self.evm = evm  # EvmLiveExecutor | None

    # --- readiness -----------------------------------------------------
    def readiness_problems(self, chain: str = "solana") -> list[str]:
        """Config-level prerequisites. Any entry here means live execution
        must refuse. Deliberately cheap and synchronous so the gateway can
        gate on it for every single trade."""
        if chain != "solana":
            if self.evm is None:
                return [f"live execution for chain {chain!r} not configured "
                        "(EVM needs TRADEOS_ZEROX_API_KEY, signer EVM key, "
                        "and a chain RPC)"]
            return self.evm.readiness_problems(chain)
        problems: list[str] = []
        if self.settings.mode != Mode.LIVE:
            problems.append(f"mode is {self.settings.mode.value}, not live")
        if self.settings.live_trading_confirm != "I_UNDERSTAND_THE_RISKS":
            problems.append("TRADEOS_LIVE_TRADING_CONFIRM not set")
        if self.registry is None or self.registry.trading_wallet(chain) is None:
            problems.append("no active trading wallet registered for chain")
        if self.signer is None or not self.signer.configured:
            problems.append("external signer not configured "
                            "(TRADEOS_SIGNER_URL / TRADEOS_SIGNER_TOKEN)")
        if self.venue is None:
            problems.append("no execution venue configured")
        if self.solana is None or not getattr(self.solana, "rpc_url", None):
            problems.append("no solana RPC configured "
                            "(TRADEOS_HELIUS_API_KEY or TRADEOS_RPC_SOLANA)")
        if self.market is None:
            problems.append("no market data provider for USD conversion")
        if self.accounting is None:
            problems.append("no accounting attached")
        return problems

    async def readiness_full(self, chain: str = "solana") -> dict:
        """Config problems plus live connectivity checks, for the readiness
        endpoint. Never called in the per-trade hot path."""
        problems = self.readiness_problems(chain)
        checks: dict[str, bool] = {}
        if chain != "solana":
            if self.evm is not None:
                if self.signer is not None and self.signer.configured:
                    checks["signer_healthy"] = await self.signer.health()
                    if not checks["signer_healthy"]:
                        problems.append("signer health check failed")
                venue_health = await self.evm.venue.health_check(chain)
                checks["venue_healthy"] = bool(venue_health.get("ok"))
                if not checks["venue_healthy"]:
                    problems.append("0x venue health check failed")
                provider = self.evm.providers.get(chain)
                if provider is not None and getattr(provider, "rpc_url", None):
                    checks["rpc_healthy"] = await provider.is_available()
                    if not checks["rpc_healthy"]:
                        problems.append(f"{chain} rpc health check failed")
            return {"ready": len(problems) == 0, "problems": problems,
                    "checks": checks}
        if self.signer is not None and self.signer.configured:
            checks["signer_healthy"] = await self.signer.health()
            if not checks["signer_healthy"]:
                problems.append("signer health check failed")
        if self.venue is not None:
            venue_health = await self.venue.health_check()
            checks["venue_healthy"] = bool(venue_health.get("ok"))
            if not checks["venue_healthy"]:
                problems.append("venue health check failed")
        if self.solana is not None and getattr(self.solana, "rpc_url", None):
            checks["rpc_healthy"] = await self.solana.is_available()
            if not checks["rpc_healthy"]:
                problems.append("solana rpc health check failed")
        return {"ready": len(problems) == 0, "problems": problems,
                "checks": checks}

    # --- helpers -------------------------------------------------------
    async def _sol_price_usd(self) -> float:
        pairs = await self.market.get_token_pairs("solana", SOL_MINT)
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
        self.db.audit("execution_live", "live_refused", instr.opportunity_id,
                      {"error": error, "side": instr.side})
        return ExecutionResult(False, trade_id=trade_id, error=error)

    # --- execution -----------------------------------------------------
    async def execute(self, instr: TradeInstruction,
                      market_price_usd: float) -> ExecutionResult:
        if instr.chain != "solana":
            if self.evm is None:
                return self._fail(
                    instr, "live prerequisites missing: "
                    + "; ".join(self.readiness_problems(instr.chain)))
            return await self.evm.execute(instr, market_price_usd)
        problems = self.readiness_problems(instr.chain)
        if problems:
            return self._fail(instr, "live prerequisites missing: "
                              + "; ".join(problems))
        wallet = self.registry.trading_wallet(instr.chain, instr.wallet_id)
        if wallet is None:
            return self._fail(instr, "no eligible trading wallet")

        sol_price = await self._sol_price_usd()
        if sol_price <= 0:
            return self._fail(instr, "SOL/USD price unavailable")

        # Priority-fee budget must fit the instruction's gas cap.
        fee_cap_lamports = self.settings.live_priority_fee_lamports_max
        fee_cap_usd = fee_cap_lamports / LAMPORTS_PER_SOL * sol_price
        if fee_cap_usd > instr.max_gas_usd:
            return self._fail(
                instr, f"priority fee cap ${fee_cap_usd:.2f} exceeds "
                f"instruction max_gas ${instr.max_gas_usd:.2f}")

        # Mint resolution and raw input amount.
        position = None
        if instr.side == "buy":
            input_mint, output_mint = SOL_MINT, instr.token_address
            amount_raw = int(instr.amount_usd / sol_price * LAMPORTS_PER_SOL)
        else:
            if instr.position_id is None:
                return self._fail(instr, "sell requires position_id")
            position = self.db.query_one(
                "SELECT * FROM positions WHERE id = ? AND status = 'open' "
                "AND mode = 'live'", (instr.position_id,))
            if position is None:
                return self._fail(instr, f"no open live position {instr.position_id}")
            decimals = await self.solana.get_token_decimals(instr.token_address)
            if decimals is None:
                return self._fail(instr, "token decimals unavailable")
            input_mint, output_mint = instr.token_address, SOL_MINT
            amount_raw = int(position["quantity"] * (10 ** decimals))
        if amount_raw <= 0:
            return self._fail(instr, "computed raw amount is zero")

        slippage_bps = int(instr.max_slippage_pct * 100)
        quote = await self.venue.quote(input_mint, output_mint, amount_raw,
                                       slippage_bps)
        if quote is None:
            return self._fail(instr, "no quote from venue")
        check = validate_quote(quote, input_mint, output_mint, slippage_bps,
                               self.settings.live_max_price_impact_pct)
        if not check.ok:
            return self._fail(instr, "quote rejected: " + "; ".join(check.reasons))

        tx_b64 = await self.venue.build_swap_transaction(
            quote, wallet["address"], fee_cap_lamports)
        if tx_b64 is None:
            return self._fail(instr, "venue did not return a transaction")

        sim = await self.solana.simulate_transaction(tx_b64)
        if not sim["ok"]:
            self.db.audit("execution_live", "simulation_failed",
                          instr.opportunity_id, sim)
            return self._fail(instr, f"simulation failed: {sim.get('error')}")

        # Estimated fill from the validated quote (reconciled on-chain later).
        if instr.side == "buy":
            decimals = await self.solana.get_token_decimals(instr.token_address)
            if decimals is None:
                return self._fail(instr, "token decimals unavailable")
            est_quantity = check.out_amount / (10 ** decimals)
            est_price = instr.amount_usd / est_quantity if est_quantity else 0
            est_filled_usd = instr.amount_usd
        else:
            est_quantity = position["quantity"]
            est_filled_usd = check.out_amount / LAMPORTS_PER_SOL * sol_price
            est_price = est_filled_usd / est_quantity if est_quantity else 0

        # Trade row BEFORE signing: from here on, an interruption leaves a
        # visible pending trade for reconciliation, never a silent unknown.
        now = time.time()
        trade_id = self.db.execute(
            "INSERT INTO trades (opportunity_id, position_id, wallet_id, chain, "
            "token_address, symbol, side, mode, requested_usd, price_usd, quantity, "
            "slippage_pct, gas_usd, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'live', ?, ?, ?, ?, ?, 'pending', ?)",
            (instr.opportunity_id, instr.position_id, wallet["id"], instr.chain,
             instr.token_address, instr.symbol, instr.side, instr.amount_usd,
             est_price, est_quantity, instr.max_slippage_pct, fee_cap_usd, now))
        self.db.audit("execution_live", "trade_submitting", instr.opportunity_id,
                      {"trade_id": trade_id, "side": instr.side,
                       "amount_raw": amount_raw, "quote_out": check.out_amount,
                       "price_impact_pct": check.price_impact_pct,
                       "simulation_units": sim.get("units_consumed")})

        try:
            signed = await self.signer.sign(
                tx_b64, instr.chain, wallet["address"],
                {"opportunity_id": instr.opportunity_id, "side": instr.side,
                 "amount_usd": instr.amount_usd, "token": instr.token_address})
        except SignerRefused as exc:
            return self._fail(instr, f"signer refused: {exc}", trade_id)
        except RuntimeError as exc:
            return self._fail(instr, str(exc), trade_id)

        signature = await self.solana.send_transaction(signed)
        if signature is None:
            # The submit RPC failed AFTER a transaction was signed: it may or
            # may not have reached the chain. Never mark failed — reconcile.
            self.db.execute(
                "UPDATE trades SET error = 'submit response lost: reconciling' "
                "WHERE id = ?", (trade_id,))
            self.db.alert("critical", "Live submit response lost",
                          f"trade {trade_id}: signed tx submitted but RPC "
                          "response lost; reconciliation running")
            return ExecutionResult(False, trade_id=trade_id,
                                   error="submit response lost; reconciling")

        self.db.execute("UPDATE trades SET tx_hash = ? WHERE id = ?",
                        (signature, trade_id))
        confirmed = await self._await_confirmation(signature)
        if confirmed is None:
            self.db.alert("high", "Live trade unconfirmed",
                          f"trade {trade_id} ({signature[:16]}…) not confirmed "
                          f"within {self.settings.live_confirm_timeout_s}s; "
                          "monitor will reconcile")
            return ExecutionResult(False, trade_id=trade_id,
                                   error="confirmation timeout; reconciling")
        if not confirmed:
            self.db.execute(
                "UPDATE trades SET status = 'failed', error = 'transaction "
                "failed on-chain' WHERE id = ?", (trade_id,))
            self.db.audit("execution_live", "tx_failed_onchain",
                          instr.opportunity_id, {"trade_id": trade_id,
                                                 "signature": signature})
            return ExecutionResult(False, trade_id=trade_id,
                                   error="transaction failed on-chain")

        return await self._finalize(trade_id, instr.opportunity_id, signature,
                                    wallet, sol_price)

    async def _await_confirmation(self, signature: str) -> bool | None:
        """True confirmed ok, False failed on-chain, None timeout."""
        deadline = time.time() + self.settings.live_confirm_timeout_s
        while time.time() < deadline:
            status = await self.solana.get_signature_status(signature)
            if status is not None:
                if status.get("err") is not None:
                    return False
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            await asyncio.sleep(2)
        return None

    # --- fill finalization ---------------------------------------------
    async def _actual_amounts(self, signature: str, wallet_address: str,
                              mint: str) -> tuple[float | None, float | None]:
        """(token_delta_ui, sol_delta) for the wallet, from on-chain balance
        changes; (None, None) when unavailable."""
        balances = await self.solana.get_transaction_balances(signature)
        if balances is None or balances.get("err") is not None:
            return None, None

        def token_total(entries):
            return sum(
                float((e.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                for e in entries
                if e.get("owner") == wallet_address and e.get("mint") == mint)

        token_delta = token_total(balances["post_token"]) - \
            token_total(balances["pre_token"])
        sol_delta = None
        keys = balances.get("account_keys") or []
        addresses = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
        if wallet_address in addresses:
            idx = addresses.index(wallet_address)
            try:
                sol_delta = (balances["post_sol"][idx]
                             - balances["pre_sol"][idx]) / LAMPORTS_PER_SOL
            except (IndexError, TypeError):
                sol_delta = None
        return (token_delta if token_delta != 0 else None), sol_delta

    async def _finalize(self, trade_id: int, opportunity_id: str | None,
                        signature: str, wallet: dict,
                        sol_price: float) -> ExecutionResult:
        trade = self.db.query_one("SELECT * FROM trades WHERE id = ?", (trade_id,))
        token_delta, sol_delta = await self._actual_amounts(
            signature, wallet["address"], trade["token_address"])
        fill_source = "onchain" if token_delta is not None else "quote_estimate"
        now = time.time()

        if trade["side"] == "buy":
            quantity = abs(token_delta) if token_delta else trade["quantity"]
            cost_usd = abs(sol_delta) * sol_price if sol_delta else trade["requested_usd"]
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
                "UPDATE trades SET status = 'filled', filled_usd = ?, price_usd = ?, "
                "quantity = ?, position_id = ?, filled_at = ? WHERE id = ?",
                (cost_usd, price, quantity, position_id, now, trade_id))
            self.accounting.record_cash("buy", -cost_usd, trade_id,
                                        f"live buy {trade['symbol']}")
            result_position = position_id
        else:
            position = self.db.query_one("SELECT * FROM positions WHERE id = ?",
                                         (trade["position_id"],))
            proceeds_usd = abs(sol_delta) * sol_price if sol_delta else \
                (trade["price_usd"] or 0) * (trade["quantity"] or 0)
            realized = proceeds_usd - position["cost_usd"]
            fill_price = proceeds_usd / position["quantity"] \
                if position["quantity"] else 0
            self.db.execute(
                "UPDATE positions SET status = 'closed', exit_price_usd = ?, "
                "exit_reason = 'live_sell', realized_pnl_usd = ?, "
                "last_price_usd = ?, closed_at = ?, updated_at = ? WHERE id = ?",
                (fill_price, realized, fill_price, now, now, position["id"]))
            self.db.execute(
                "UPDATE trades SET status = 'filled', filled_usd = ?, price_usd = ?, "
                "filled_at = ? WHERE id = ?",
                (proceeds_usd, fill_price, now, trade_id))
            self.accounting.record_cash("sell", proceeds_usd, trade_id,
                                        f"live sell {trade['symbol']}")
            result_position = position["id"]

        self.db.audit("execution_live", "trade_filled", opportunity_id, {
            "trade_id": trade_id, "signature": signature,
            "fill_source": fill_source,
            "token_delta": token_delta, "sol_delta": sol_delta})
        self.db.alert("high", f"LIVE {trade['side']} filled: {trade['symbol']}",
                      f"tx {signature[:20]}… (fill: {fill_source})")
        return ExecutionResult(True, trade_id=trade_id,
                               position_id=result_position,
                               fill_price=self.db.query_one(
                                   "SELECT price_usd FROM trades WHERE id = ?",
                                   (trade_id,))["price_usd"])

    # --- reconciliation -------------------------------------------------
    async def reconcile_pending(self) -> int:
        """Resolve live trades stuck in 'pending' (crash, lost response,
        confirmation timeout). Called by the monitor loop and at startup."""
        resolved = 0
        if self.evm is not None:
            resolved += await self.evm.reconcile_pending()
        if self.registry is None or self.solana is None:
            return resolved  # solana leg unwired; nothing it could resolve
        pending = self.db.query(
            "SELECT * FROM trades WHERE mode = 'live' AND status = 'pending' "
            "AND chain = 'solana'")
        for trade in pending:
            if not trade["tx_hash"]:
                # Signed (maybe) but no signature recorded: cannot ever
                # resolve automatically — needs the operator.
                if time.time() - trade["created_at"] > 300:
                    self.db.execute(
                        "UPDATE trades SET status = 'failed', error = "
                        "'no signature recorded: verify wallet manually' "
                        "WHERE id = ?", (trade["id"],))
                    self.db.alert("critical", "Unresolvable live trade",
                                  f"trade {trade['id']}: no signature; check "
                                  "the wallet's on-chain history manually")
                    resolved += 1
                continue
            status = await self.solana.get_signature_status(trade["tx_hash"])
            if status is None:
                if time.time() - trade["created_at"] > 600:
                    self.db.execute(
                        "UPDATE trades SET status = 'failed', error = "
                        "'never landed on-chain (blockhash expired)' WHERE id = ?",
                        (trade["id"],))
                    self.db.audit("execution_live", "reconciled_dropped", None,
                                  {"trade_id": trade["id"]})
                    resolved += 1
                continue
            if status.get("err") is not None:
                self.db.execute(
                    "UPDATE trades SET status = 'failed', error = "
                    "'transaction failed on-chain' WHERE id = ?", (trade["id"],))
                resolved += 1
                continue
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                wallet = self.registry.get(trade["wallet_id"]) if trade["wallet_id"] \
                    else self.registry.trading_wallet(trade["chain"])
                sol_price = await self._sol_price_usd()
                if wallet and sol_price > 0:
                    await self._finalize(trade["id"], trade["opportunity_id"],
                                         trade["tx_hash"], wallet, sol_price)
                    resolved += 1
        return resolved
