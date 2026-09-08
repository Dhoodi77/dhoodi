"""Smart-money discovery scanner.

Runs the discovery process from the spec on real indexer data:

  wallet discovery (active traders on tokens the system cares about)
  -> historical transaction analysis (Helius parsed swap history)
  -> performance analysis (SOL round trips, average cost basis)
  -> scoring with sample-size confidence and time decay
  -> continuous monitoring (re-scored when stale)

Everything it stores is derived from indexer data; wallets with no
completed round trips in the window are simply not scored.
"""
from __future__ import annotations

import logging
import time

from tradeos.config import Settings
from tradeos.db.database import Database
from tradeos.providers.swaps import SwapRecord
from tradeos.wallets.analysis import analyze_wallet_swaps
from tradeos.wallets.reputation import WalletReputationStore

logger = logging.getLogger(__name__)

RESCORE_AFTER_S = 6 * 3600


class SmartMoneyScanner:
    """Chain-agnostic: works with any swap source exposing
    get_address_swaps(wallet) and optionally token_activity(mint)
    (HeliusProvider for Solana, EtherscanSwapSource per EVM chain)."""

    def __init__(self, settings: Settings, db: Database, swap_source,
                 reputation: WalletReputationStore, chain: str = "solana"):
        self.settings = settings
        self.db = db
        self.swap_source = swap_source
        self.reputation = reputation
        self.chain = chain

    # --- persistence ---------------------------------------------------
    def store_swaps(self, records: list[SwapRecord]) -> int:
        stored = 0
        for r in records:
            stored += self.db.execute_rowcount(
                "INSERT OR IGNORE INTO wallet_swaps (chain, wallet, signature, "
                "token_mint, direction, token_amount, sol_amount, counter_mint, "
                "block_time, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r.chain, r.wallet, r.signature, r.token_mint, r.direction,
                 r.token_amount, r.sol_amount, r.counter_mint, r.block_time,
                 time.time()))
        return stored

    # --- candidate selection -------------------------------------------
    def candidate_tokens(self, limit: int = 5) -> list[str]:
        """Tokens worth watching: open Solana positions first, then the most
        recent Solana opportunities."""
        mints: list[str] = []
        for row in self.db.query(
                "SELECT DISTINCT token_address FROM positions "
                "WHERE status = 'open' AND chain = ?", (self.chain,)):
            mints.append(row["token_address"])
        for row in self.db.query(
                "SELECT token_address FROM opportunities WHERE chain = ? "
                "AND created_at > ? ORDER BY created_at DESC LIMIT 20",
                (self.chain, time.time() - 24 * 3600)):
            if row["token_address"] not in mints:
                mints.append(row["token_address"])
        return mints[:limit]

    def _needs_scoring(self, wallet: str) -> bool:
        scored = self.db.query_one(
            "SELECT scored_at FROM wallet_scores WHERE chain = ? AND address = ? "
            "ORDER BY scored_at DESC LIMIT 1", (self.chain, wallet))
        return scored is None or time.time() - scored["scored_at"] > RESCORE_AFTER_S

    def candidate_wallets(self, mints: list[str],
                          extra: list[str] = ()) -> list[str]:
        """Wallets that recently traded the candidate tokens (most active
        first, from stored swaps), plus extra candidates from the source,
        excluding recently scored ones."""
        ordered: list[str] = []
        if mints:
            placeholders = ",".join("?" for _ in mints)
            rows = self.db.query(
                f"SELECT wallet, COUNT(*) AS n FROM wallet_swaps "
                f"WHERE chain = ? AND token_mint IN ({placeholders}) "
                f"AND block_time >= ? GROUP BY wallet ORDER BY n DESC LIMIT 50",
                (self.chain, *mints, time.time() - 48 * 3600))
            ordered = [row["wallet"] for row in rows]
        for wallet in extra:
            if wallet not in ordered:
                ordered.append(wallet)
        return [w for w in ordered if self._needs_scoring(w)]

    def discover_candidate_mints(self, since_hours: float = 6.0,
                                 limit: int = 5) -> list[str]:
        """Discovery flywheel: mints that tracked smart-money wallets bought
        recently and the system has not yet analyzed. The scheduler feeds
        these into the opportunity pipeline, so discovery is no longer
        limited to tokens DexScreener promotes."""
        tracked = self.reputation.tracked_smart_money(
            min_score=self.settings.smartmoney_score_threshold)
        wallets = [t["address"] for t in tracked][:20]
        if not wallets:
            return []
        placeholders = ",".join("?" for _ in wallets)
        rows = self.db.query(
            f"SELECT DISTINCT token_mint FROM wallet_swaps WHERE chain = ? "
            f"AND wallet IN ({placeholders}) AND direction = 'buy' "
            f"AND block_time >= ?",
            (self.chain, *wallets, time.time() - since_hours * 3600))
        candidates: list[str] = []
        for row in rows:
            seen = self.db.query_one(
                "SELECT id FROM opportunities WHERE chain = ? AND token_address = ? "
                "AND created_at > ?",
                (self.chain, row["token_mint"], time.time() - 24 * 3600))
            if seen is None:
                candidates.append(row["token_mint"])
        return candidates[:limit]

    async def _token_activity(self, mint: str):
        """(records, candidate wallets) for a token. Sources without a
        token_activity method (Helius-style) derive both from the token's
        own parsed swap feed."""
        if hasattr(self.swap_source, "token_activity"):
            return await self.swap_source.token_activity(mint)
        records = await self.swap_source.get_address_swaps(mint)
        return records, [r.wallet for r in records]

    # --- scan ----------------------------------------------------------
    async def scan(self) -> dict:
        """One scan pass. Returns counters plus flywheel candidates."""
        stats = {"chain": self.chain, "tokens": 0, "swaps_stored": 0,
                 "wallets_analyzed": 0, "wallets_scored": 0,
                 "candidate_mints": []}
        mints = self.candidate_tokens()
        traders: list[str] = []
        for mint in mints:
            records, wallets = await self._token_activity(mint)
            stats["swaps_stored"] += self.store_swaps(records)
            traders += [w for w in wallets if w not in traders]
            stats["tokens"] += 1

        for wallet in self.candidate_wallets(mints, traders)[
                : self.settings.smartmoney_max_wallets_per_scan]:
            records = await self.swap_source.get_address_swaps(wallet)
            self.store_swaps(records)
            stats["wallets_analyzed"] += 1
            history = self.db.query(
                "SELECT token_mint, direction, token_amount, sol_amount, "
                "counter_mint, block_time FROM wallet_swaps "
                "WHERE chain = ? AND wallet = ? ORDER BY block_time",
                (self.chain, wallet))
            perf = analyze_wallet_swaps(history)
            if perf is None or perf.trade_count < self.settings.smartmoney_min_wallet_trades:
                continue
            score = self.reputation.record_score(self.chain, wallet, perf)
            stats["wallets_scored"] += 1
            if score >= self.settings.smartmoney_score_threshold:
                self.db.alert(
                    "info", f"Smart-money wallet identified: {wallet[:8]}…",
                    f"score {score}, {perf.trade_count} round trips, "
                    f"win rate {perf.win_rate:.0%}, avg {perf.avg_return_pct:+.1f}%")
        stats["candidate_mints"] = self.discover_candidate_mints()
        self.db.kv_set("smartmoney_last_scan", str(time.time()))
        logger.info("smart-money scan: %s", stats)
        return stats
