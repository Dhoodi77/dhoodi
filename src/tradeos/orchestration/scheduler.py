"""Autonomous scheduler: discovery and monitoring loops with recovery.

Runs as asyncio tasks inside the API process. Every loop iteration is
wrapped so a provider outage or bug degrades to a logged system event and a
retry, never a crashed process. Process-level supervision (systemd restart)
is documented in docs/OPERATIONS.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from tradeos.config import Mode, Settings
from tradeos.db.database import Database
from tradeos.orchestration.monitor import PositionMonitor
from tradeos.orchestration.pipeline import OpportunityPipeline
from tradeos.providers.dexscreener import DexScreenerProvider

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, settings: Settings, db: Database, market: DexScreenerProvider,
                 pipeline: OpportunityPipeline, monitor: PositionMonitor,
                 smartmoney_scanner=None, webhook_manager=None):
        self.settings = settings
        self.db = db
        self.market = market
        self.pipeline = pipeline
        self.monitor = monitor
        self.smartmoney_scanner = smartmoney_scanner
        self.webhook_manager = webhook_manager
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self.startup_recovery()
        self._tasks = [
            asyncio.create_task(self._loop("discovery", self._discovery_tick,
                                           self.settings.discovery_interval_s)),
            asyncio.create_task(self._loop("monitor", self._monitor_tick,
                                           self.settings.monitor_interval_s)),
            asyncio.create_task(self._startup_health_checks()),
        ]
        if self.smartmoney_scanner is not None:
            self._tasks.append(asyncio.create_task(
                self._loop("smartmoney", self._smartmoney_tick,
                           self.settings.smartmoney_scan_interval_s)))
        self.db.system_event("scheduler_started",
                             f"mode={self.settings.mode.value}")

    async def _startup_health_checks(self) -> None:
        """Verify external providers actually respond with the expected
        shapes so API drift or blocked egress is loud, not silent."""
        results: dict[str, dict] = {}
        try:
            pairs = await self.market.search("SOL")
            results["dexscreener"] = {"ok": bool(pairs),
                                      "pairs_returned": len(pairs)}
        except Exception as exc:
            results["dexscreener"] = {"ok": False, "error": type(exc).__name__}
        if self.smartmoney_scanner is not None:
            try:
                results["helius"] = await self.smartmoney_scanner.helius.health_check()
            except Exception as exc:
                results["helius"] = {"ok": False, "error": type(exc).__name__}
        self.db.kv_set("provider_health", json.dumps(results))
        failed = [name for name, r in results.items() if not r.get("ok")]
        if failed:
            self.db.alert(
                "critical", f"Provider health check FAILED: {', '.join(failed)}",
                json.dumps(results))
            self.db.system_event("provider_health_failed", json.dumps(results))
        else:
            self.db.system_event("provider_health_ok", json.dumps(results))
        if self.webhook_manager is not None:
            await self.webhook_manager.sync()

    async def stop(self) -> None:
        self._stopping.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.db.system_event("scheduler_stopped", "graceful shutdown")

    def startup_recovery(self) -> None:
        """Reconcile state after a restart. Never assume an interrupted
        process means an interrupted trade: pending trades older than the
        restart are marked unknown for review; open positions stay open and
        the monitor re-verifies them against live prices."""
        pending = self.db.query("SELECT * FROM trades WHERE status = 'pending'")
        for t in pending:
            self.db.execute(
                "UPDATE trades SET status = 'failed', "
                "error = 'unresolved at restart: requires reconciliation' WHERE id = ?",
                (t["id"],))
            self.db.alert("critical", "Pending trade found at startup",
                          f"trade {t['id']} ({t['symbol']}) marked for reconciliation")
        open_positions = self.db.query(
            "SELECT COUNT(*) AS n FROM positions WHERE status = 'open'")
        self.db.system_event(
            "startup_recovery",
            f"pending_trades_flagged={len(pending)} "
            f"open_positions={open_positions[0]['n'] if open_positions else 0}")
        self.db.kv_set("last_startup", str(time.time()))

    async def _loop(self, name: str, tick, interval_s: int) -> None:
        while not self._stopping.is_set():
            started = time.time()
            try:
                await tick()
                self.db.kv_set(f"loop_ok_{name}", str(time.time()))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s loop iteration failed", name)
                self.db.system_event("loop_error", name)
            elapsed = time.time() - started
            try:
                await asyncio.wait_for(self._stopping.wait(),
                                       timeout=max(1.0, interval_s - elapsed))
            except asyncio.TimeoutError:
                pass

    async def _discovery_tick(self) -> None:
        if self.settings.mode == Mode.DEVELOPMENT:
            return  # observation only in development would still cost rate limit
        pairs = await self.market.discover()
        logger.info("discovery found %d candidate pairs", len(pairs))
        for pair in pairs[:15]:  # bound per-tick agent/LLM spend
            await self.pipeline.process(pair)

    async def _monitor_tick(self) -> None:
        closed = await self.monitor.check_all()
        if closed:
            logger.info("monitor closed %d positions", closed)

    async def _smartmoney_tick(self) -> None:
        if self.settings.mode == Mode.DEVELOPMENT:
            return
        stats = await self.smartmoney_scanner.scan()
        # Discovery flywheel: analyze tokens tracked smart money just bought.
        for mint in stats.get("candidate_mints", []):
            pairs = await self.market.get_token_pairs("solana", mint)
            if pairs:
                await self.pipeline.process(max(pairs, key=lambda p: p.liquidity_usd))
        # Keep the webhook's tracked-wallet list current with new scores.
        if self.webhook_manager is not None:
            await self.webhook_manager.sync()
