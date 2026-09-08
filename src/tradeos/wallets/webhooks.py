"""Helius webhook management and real-time event handling.

Registration (sync): keeps one Helius *enhanced* webhook pointed at
``{TRADEOS_PUBLIC_URL}/webhooks/helius`` whose address list is the current
set of tracked smart-money wallets. Re-synced after every scanner pass so
newly identified wallets start streaming without a restart. Requires both
TRADEOS_PUBLIC_URL and TRADEOS_HELIUS_WEBHOOK_SECRET; without them the
system stays on polling and says so once in the log.

Delivery (handle_payload): Helius POSTs the same parsed-transaction shape
as the Enhanced Transactions API, so the battle-tested parse_enhanced_swap
is reused. Reactions are bounded and deterministic:
  - every valid SOL<->token swap is stored (deduplicated by signature)
  - a *buy* by a tracked smart-money wallet raises a high-priority alert
    and, if the token is not already active, feeds it straight into the
    opportunity pipeline — the same pipeline with the same risk gates;
    a webhook can accelerate analysis but never bypass anything.
  - a *sell* by a tracked wallet of a token we hold a position in raises
    an alert (position requires attention).
"""
from __future__ import annotations

import logging
import time

from tradeos.config import Settings
from tradeos.db.database import Database
from tradeos.providers.chains.helius import HeliusProvider, parse_enhanced_swap
from tradeos.wallets.reputation import WalletReputationStore
from tradeos.wallets.scanner import SmartMoneyScanner

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/webhooks/helius"


class HeliusWebhookManager:
    chain = "solana"

    def __init__(self, settings: Settings, db: Database, helius: HeliusProvider,
                 reputation: WalletReputationStore, scanner: SmartMoneyScanner,
                 pipeline=None, market=None):
        self.settings = settings
        self.db = db
        self.helius = helius
        self.reputation = reputation
        self.scanner = scanner
        self.pipeline = pipeline
        self.market = market
        self._warned_unconfigured = False

    # --- registration --------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.settings.public_url and self.settings.helius_webhook_secret)

    def webhook_url(self) -> str:
        return self.settings.public_url.rstrip("/") + WEBHOOK_PATH

    def tracked_addresses(self) -> list[str]:
        tracked = self.reputation.tracked_smart_money(
            min_score=self.settings.smartmoney_score_threshold)
        return [t["address"] for t in tracked][: self.settings.webhook_max_addresses]

    async def sync(self) -> bool:
        """Ensure the Helius webhook exists and tracks the current wallet
        set. Returns True when a webhook is registered and current."""
        if not self.configured:
            if not self._warned_unconfigured:
                logger.info(
                    "helius webhooks disabled: set TRADEOS_PUBLIC_URL and "
                    "TRADEOS_HELIUS_WEBHOOK_SECRET to enable real-time tracking")
                self._warned_unconfigured = True
            return False
        addresses = self.tracked_addresses()
        if not addresses:
            return False  # nothing to track yet; polling continues

        hooks = await self.helius.list_webhooks()
        if hooks is None:
            self.db.system_event("webhook_sync_failed", "could not list webhooks")
            return False

        url = self.webhook_url()
        secret = self.settings.helius_webhook_secret
        mine = next((h for h in hooks if h.get("webhookURL") == url), None)
        if mine is None:
            created = await self.helius.create_webhook(url, addresses, secret)
            if created is None:
                self.db.system_event("webhook_sync_failed", "create failed")
                return False
            self.db.kv_set("helius_webhook_id", str(created.get("webhookID", "")))
            self.db.system_event("webhook_created",
                                 f"{len(addresses)} tracked wallets")
        elif set(mine.get("accountAddresses") or []) != set(addresses):
            updated = await self.helius.update_webhook(
                str(mine.get("webhookID", "")), url, addresses, secret)
            if updated is None:
                self.db.system_event("webhook_sync_failed", "update failed")
                return False
            self.db.system_event("webhook_updated",
                                 f"{len(addresses)} tracked wallets")
        self.db.kv_set("helius_webhook_synced_at", str(time.time()))
        return True

    # --- delivery ------------------------------------------------------
    async def handle_payload(self, payload: list[dict]) -> dict:
        records = [r for r in (parse_enhanced_swap(tx) for tx in payload) if r]
        stored = self.scanner.store_swaps(records)
        self.db.kv_set("helius_webhook_last_event", str(time.time()))

        tracked_scores = self.reputation.effective_scores(
            self.chain, list({r.wallet for r in records}))
        threshold = self.settings.smartmoney_score_threshold
        reactions = 0
        for record in records:
            score = tracked_scores.get(record.wallet)
            if score is None or score < threshold:
                continue
            short = f"{record.wallet[:8]}…"
            if record.direction == "buy":
                reactions += 1
                self.db.alert(
                    "high", f"Smart money bought {record.token_mint[:8]}…",
                    f"wallet {short} (score {score:.0f}) spent "
                    f"{record.sol_amount:.2f} SOL")
                self.db.audit("webhook", "smartmoney_buy_event", None, {
                    "wallet": record.wallet, "mint": record.token_mint,
                    "sol": record.sol_amount, "score": score})
                await self._maybe_analyze(record.token_mint)
            else:
                position = self.db.query_one(
                    "SELECT id FROM positions WHERE status = 'open' "
                    "AND chain = ? AND token_address = ?",
                    (self.chain, record.token_mint))
                if position is not None:
                    reactions += 1
                    self.db.alert(
                        "critical",
                        f"Smart money SOLD a token we hold: {record.token_mint[:8]}…",
                        f"wallet {short} (score {score:.0f}) sold for "
                        f"{record.sol_amount:.2f} SOL; position {position['id']} "
                        "may require attention")
        return {"received": len(payload), "parsed": len(records),
                "stored": stored, "reactions": reactions}

    async def _maybe_analyze(self, mint: str) -> None:
        """Feed a smart-money buy into the normal pipeline (which does its
        own dedup, scoring, agents, and risk gating)."""
        if self.pipeline is None or self.market is None:
            return
        try:
            pairs = await self.market.get_token_pairs(self.chain, mint)
            if pairs:
                best = max(pairs, key=lambda p: p.liquidity_usd)
                await self.pipeline.process(best)
        except Exception:
            logger.exception("webhook-triggered analysis failed for %s", mint)
