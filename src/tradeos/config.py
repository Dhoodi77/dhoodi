"""Central configuration.

All financially significant parameters live here, loaded from environment /
.env. Nothing trading-critical is buried in business logic. The risk policy
is intentionally all-Optional: the RiskEngine refuses to trade unless every
field is explicitly configured (see risk/policy.py).
"""
from __future__ import annotations

import enum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Hard, code-level constraint required by product policy: no newly opened
# position may exceed this many USD regardless of configuration or any AI
# output. Configuration may only lower it, never raise it.
HARD_INITIAL_TRADE_CAP_USD = 20.0


class Mode(str, enum.Enum):
    DEVELOPMENT = "development"
    PAPER = "paper"
    LIVE = "live"


class RiskConfig(BaseSettings):
    """Raw risk parameters as configured. None = not configured = no trading.

    Reads TRADEOS_RISK_* env vars directly (its own prefix, because
    pydantic-settings' nested delimiter cannot disambiguate underscores in
    field names like max_daily_loss_usd).
    """

    model_config = SettingsConfigDict(
        env_prefix="TRADEOS_RISK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    max_initial_position_usd: float | None = None
    max_position_usd: float | None = None
    max_daily_loss_usd: float | None = None
    max_portfolio_exposure_usd: float | None = None
    max_slippage_pct: float | None = None
    max_gas_usd: float | None = None
    max_open_positions: int | None = None
    max_drawdown_pct: float | None = None
    emergency_stop_loss_usd: float | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRADEOS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mode: Mode = Mode.DEVELOPMENT

    # API / dashboard
    api_host: str = "127.0.0.1"
    api_port: int = 8420
    dashboard_token: str | None = None

    database_path: str = "./data/tradeos.db"

    # Model routing: role -> Anthropic model id. anthropic_api_key is read
    # from the standard ANTHROPIC_API_KEY env var by the SDK itself; we only
    # detect presence, never store or log it.
    model_fast: str = "claude-haiku-4-5"
    model_reasoning: str = "claude-opus-5"
    model_decision: str = "claude-opus-5"

    # Risk policy (flattened by env_nested_delimiter: TRADEOS_RISK_*)
    risk: RiskConfig = Field(default_factory=RiskConfig)

    # Trading universe
    allowed_chains: str = "solana,ethereum,base,bsc,arbitrum,polygon"
    allowed_dexes: str = ""  # empty = any DEX reported by data provider
    min_liquidity_usd: float = 25000.0
    min_volume_24h_usd: float = 50000.0
    min_token_age_hours: float = 1.0

    # Paper trading
    paper_starting_balance_usd: float = 500.0
    paper_simulated_slippage_pct: float = 1.0

    # Exit rules (momentum v1)
    exit_stop_loss_pct: float = 15.0
    exit_take_profit_pct: float = 30.0
    exit_max_hold_hours: float = 24.0

    # Scheduler
    discovery_interval_s: int = 120
    monitor_interval_s: int = 30

    # Smart-money scanner (active only when TRADEOS_HELIUS_API_KEY is set)
    smartmoney_scan_interval_s: int = 600
    smartmoney_max_wallets_per_scan: int = 8
    smartmoney_min_wallet_trades: int = 3
    smartmoney_score_threshold: float = 65.0
    whale_sol_threshold: float = 50.0
    whale_usd_threshold: float = 5000.0  # EVM whale swaps (stable-denominated)

    # Helius webhooks (real-time tracked-wallet events). Both must be set for
    # webhook registration: the public HTTPS base URL this instance is
    # reachable at, and a shared secret Helius echoes in the Authorization
    # header of every delivery.
    public_url: str | None = None
    helius_webhook_secret: str | None = None
    webhook_max_addresses: int = 100

    # Live trading gate
    live_trading_confirm: str = ""

    # Live execution (Solana/Jupiter). Signer credentials come from
    # TRADEOS_SIGNER_URL / TRADEOS_SIGNER_TOKEN (read by the signer client).
    live_max_price_impact_pct: float = 2.0
    live_priority_fee_lamports_max: int = 1_000_000  # 0.001 SOL
    live_confirm_timeout_s: float = 60.0

    @property
    def allowed_chain_list(self) -> list[str]:
        return [c.strip().lower() for c in self.allowed_chains.split(",") if c.strip()]

    @property
    def allowed_dex_list(self) -> list[str]:
        return [d.strip().lower() for d in self.allowed_dexes.split(",") if d.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
