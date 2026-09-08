"""Kill switch: outside the AI layer, blocks execution, resume rules."""
import os

from tradeos.db.database import Database
from tradeos.execution.instructions import TradeInstruction
from tradeos.risk.killswitch import KillSwitch


def instr() -> TradeInstruction:
    return TradeInstruction(opportunity_id="opp_ks", chain="solana",
                            token_address="Tok", side="buy", amount_usd=10,
                            max_slippage_pct=1, max_gas_usd=1)


def test_activate_blocks_gateway(gateway, kill_switch):
    kill_switch.activate("test", "unit test")
    result = gateway.submit(instr(), market_price_usd=1.0)
    assert not result.ok
    assert "kill switch" in result.error


def test_activation_is_recorded(db, kill_switch):
    kill_switch.activate("test", "audit check")
    events = db.query("SELECT * FROM risk_events WHERE kind = 'kill_switch'")
    assert len(events) == 1
    alerts = db.query("SELECT * FROM alerts WHERE priority = 'critical'")
    assert len(alerts) == 1
    assert kill_switch.reason() == "audit check"


def test_deactivate(kill_switch):
    kill_switch.activate("test", "x")
    assert kill_switch.is_active()
    kill_switch.deactivate("test")
    assert not kill_switch.is_active()


def test_file_sentinel_activates(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    ks = KillSwitch(db)
    assert not ks.is_active()
    (tmp_path / "KILLSWITCH").touch()
    assert ks.is_active()
    # API-level deactivate must refuse while the file sentinel exists
    try:
        ks.deactivate("test")
        raised = False
    except RuntimeError:
        raised = True
    assert raised
    os.remove(tmp_path / "KILLSWITCH")
    ks.deactivate("test")
    assert not ks.is_active()
    db.close()
