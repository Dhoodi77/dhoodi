import pytest

from tradeos.config import Mode, RiskConfig, Settings
from tradeos.db.database import Database
from tradeos.execution.gateway import ExecutionGateway
from tradeos.portfolio.accounting import PortfolioAccounting
from tradeos.risk.engine import RiskEngine
from tradeos.risk.killswitch import KillSwitch

FULL_RISK = RiskConfig(
    max_initial_position_usd=20,
    max_position_usd=20,
    max_daily_loss_usd=40,
    max_portfolio_exposure_usd=100,
    max_slippage_pct=3.0,
    max_gas_usd=2.0,
    max_open_positions=5,
    max_drawdown_pct=25,
    emergency_stop_loss_usd=80,
)


def make_settings(**overrides) -> Settings:
    defaults = dict(
        mode=Mode.PAPER,
        risk=FULL_RISK,
        database_path=":memory:",
        dashboard_token="test-token",
        paper_starting_balance_usd=500.0,
        paper_simulated_slippage_pct=1.0,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def db() -> Database:
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def kill_switch(db) -> KillSwitch:
    return KillSwitch(db)


@pytest.fixture
def accounting(db, settings) -> PortfolioAccounting:
    acct = PortfolioAccounting(db, mode="paper")
    acct.ensure_seeded(settings.paper_starting_balance_usd)
    return acct


@pytest.fixture
def risk_engine(settings, db, kill_switch) -> RiskEngine:
    return RiskEngine(settings, db, kill_switch)


@pytest.fixture
def gateway(settings, db, risk_engine, kill_switch, accounting) -> ExecutionGateway:
    return ExecutionGateway(settings, db, risk_engine, kill_switch, accounting)
