"""Failure and recovery: restarts, provider outages, DB reconstruction."""
import pytest

from tradeos.db.database import Database
from tradeos.execution.instructions import TradeInstruction
from tradeos.memory.stores import MemoryStore
from tradeos.orchestration.scheduler import Scheduler
from tradeos.portfolio.accounting import PortfolioAccounting


def test_state_survives_reopen(tmp_path, settings, kill_switch):
    """Everything reconstructs from the database file after a 'restart'."""
    from tradeos.execution.gateway import ExecutionGateway
    from tradeos.risk.engine import RiskEngine
    from tradeos.risk.killswitch import KillSwitch

    path = str(tmp_path / "restart.db")
    db1 = Database(path)
    acct1 = PortfolioAccounting(db1, "paper")
    acct1.ensure_seeded(500)
    ks1 = KillSwitch(db1)
    gw1 = ExecutionGateway(settings, db1, RiskEngine(settings, db1, ks1), ks1, acct1)
    result = gw1.submit(TradeInstruction(
        opportunity_id="opp_r", chain="solana", token_address="tokR", symbol="R",
        side="buy", amount_usd=20, max_slippage_pct=2, max_gas_usd=1), 2.0)
    assert result.ok
    db1.close()

    db2 = Database(path)
    acct2 = PortfolioAccounting(db2, "paper")
    assert acct2.cash_usd() == 480.0
    positions = acct2.open_positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "R"
    state = acct2.state()
    assert state.open_positions == 1
    assert state.exposure_usd == 20.0
    db2.close()


def test_seeding_is_idempotent(tmp_path):
    db = Database(str(tmp_path / "seed.db"))
    acct = PortfolioAccounting(db, "paper")
    acct.ensure_seeded(500)
    acct.ensure_seeded(500)
    acct.ensure_seeded(500)
    assert acct.cash_usd() == 500.0
    db.close()


def test_startup_recovery_flags_pending_trades(settings, db):
    import time
    db.execute(
        "INSERT INTO trades (chain, token_address, symbol, side, mode, requested_usd, "
        "status, created_at) VALUES ('solana', 't', 'X', 'buy', 'paper', 20, "
        "'pending', ?)", (time.time(),))
    scheduler = Scheduler(settings, db, None, None, None)
    scheduler.startup_recovery()
    trade = db.query_one("SELECT * FROM trades")
    assert trade["status"] == "failed"
    assert "reconciliation" in trade["error"]
    alerts = db.query("SELECT * FROM alerts WHERE priority = 'critical'")
    assert len(alerts) == 1


async def test_provider_outage_degrades_gracefully(settings, db, gateway):
    """Monitor sweep with a dead market provider must not crash or exit
    positions blindly."""
    from tradeos.llm.client import LlmClient
    from tradeos.orchestration.monitor import PositionMonitor

    result = gateway.submit(TradeInstruction(
        opportunity_id="opp_o", chain="solana", token_address="tokO",
        pair_address="pairO", symbol="O", side="buy", amount_usd=20,
        max_slippage_pct=2, max_gas_usd=1), 1.0)
    assert result.ok

    class DeadMarket:
        async def get_pair(self, *a):
            raise RuntimeError("network down")

        async def get_token_pairs(self, *a):
            raise RuntimeError("network down")

    llm = LlmClient(settings)
    llm.available = False
    llm._client = None
    monitor = PositionMonitor(settings, db, DeadMarket(), gateway, llm)
    with pytest.raises(RuntimeError):
        # individual position errors propagate to the scheduler loop wrapper,
        # which logs and retries next tick; position must remain open
        await monitor.check_all()
    pos = db.query_one("SELECT * FROM positions WHERE id = ?", (result.position_id,))
    assert pos["status"] == "open"


def test_memory_never_stores_secret_shapes(db):
    store = MemoryStore(db)
    store.remember("user", "pref", "my key is sk-ant-abc123def456ghi789jkl ok")
    content = store.recall("user", "pref")
    assert "sk-ant" not in content
    assert "[REDACTED]" in content


def test_memory_rejects_unknown_layer(db):
    store = MemoryStore(db)
    with pytest.raises(ValueError):
        store.remember("secrets", "k", "v")
