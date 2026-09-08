"""Risk engine: policy enforcement, hard caps, circuit breakers."""
import pytest

from tradeos.config import HARD_INITIAL_TRADE_CAP_USD, Mode, RiskConfig
from tradeos.execution.instructions import TradeInstruction
from tradeos.risk.engine import PortfolioState, RiskEngine
from tradeos.risk.killswitch import KillSwitch
from tradeos.risk.policy import RiskPolicy

from .conftest import FULL_RISK, make_settings


def make_instr(**overrides) -> TradeInstruction:
    defaults = dict(
        opportunity_id="opp_test", chain="solana", token_address="TokenAAA",
        side="buy", amount_usd=20.0, max_slippage_pct=2.0, max_gas_usd=1.0,
    )
    defaults.update(overrides)
    return TradeInstruction(**defaults)


def healthy_state() -> PortfolioState:
    return PortfolioState(cash_usd=500, open_positions=0, exposure_usd=0,
                          daily_pnl_usd=0, total_pnl_usd=0,
                          peak_equity_usd=500, equity_usd=500)


# --- policy construction -------------------------------------------------

def test_missing_risk_params_block_trading(db, kill_switch):
    settings = make_settings(risk=RiskConfig())  # nothing configured
    engine = RiskEngine(settings, db, kill_switch)
    assert engine.policy is None
    allowed, problems = engine.trading_allowed()
    assert not allowed
    assert any("not configured" in p for p in problems)


def test_partial_risk_params_block_trading(db, kill_switch):
    cfg = FULL_RISK.model_copy(update={"max_daily_loss_usd": None})
    engine = RiskEngine(make_settings(risk=cfg), db, kill_switch)
    assert engine.policy is None


def test_config_cannot_raise_hard_initial_cap():
    cfg = FULL_RISK.model_copy(update={"max_initial_position_usd": 1000.0})
    policy, errors = RiskPolicy.from_config(cfg)
    assert errors == []
    assert policy.max_initial_position_usd == HARD_INITIAL_TRADE_CAP_USD


def test_config_can_lower_initial_cap():
    cfg = FULL_RISK.model_copy(update={"max_initial_position_usd": 5.0})
    policy, _ = RiskPolicy.from_config(cfg)
    assert policy.max_initial_position_usd == 5.0


# --- per-trade limits ----------------------------------------------------

def test_buy_over_hard_cap_rejected(risk_engine):
    decision = risk_engine.evaluate_trade(make_instr(amount_usd=1000.0), healthy_state())
    assert not decision.approved
    assert decision.rule in ("hard_initial_cap", "max_initial_position")


def test_llm_says_buy_1000_system_rejects(risk_engine):
    """The spec's canonical case: Claude says buy $1000, policy says $20."""
    decision = risk_engine.evaluate_trade(make_instr(amount_usd=1000.0), healthy_state())
    assert not decision.approved


def test_valid_buy_approved(risk_engine):
    decision = risk_engine.evaluate_trade(make_instr(), healthy_state())
    assert decision.approved


def test_excess_slippage_rejected(risk_engine):
    decision = risk_engine.evaluate_trade(make_instr(max_slippage_pct=10.0), healthy_state())
    assert not decision.approved
    assert decision.rule == "max_slippage"


def test_excess_gas_rejected(risk_engine):
    decision = risk_engine.evaluate_trade(make_instr(max_gas_usd=50.0), healthy_state())
    assert not decision.approved
    assert decision.rule == "max_gas"


def test_disallowed_chain_rejected(risk_engine):
    decision = risk_engine.evaluate_trade(make_instr(chain="tron"), healthy_state())
    assert not decision.approved
    assert decision.rule == "chain_not_allowed"


def test_max_open_positions_enforced(risk_engine):
    state = healthy_state()
    state.open_positions = 5
    decision = risk_engine.evaluate_trade(make_instr(), state)
    assert not decision.approved
    assert decision.rule == "max_open_positions"


def test_portfolio_exposure_enforced(risk_engine):
    state = healthy_state()
    state.exposure_usd = 95.0
    decision = risk_engine.evaluate_trade(make_instr(amount_usd=20.0), state)
    assert not decision.approved
    assert decision.rule == "max_portfolio_exposure"


def test_insufficient_cash_rejected(risk_engine):
    state = healthy_state()
    state.cash_usd = 5.0
    decision = risk_engine.evaluate_trade(make_instr(), state)
    assert not decision.approved
    assert decision.rule == "insufficient_cash"


# --- circuit breakers ----------------------------------------------------

def test_daily_loss_breaker_trips_and_activates_kill_switch(risk_engine, kill_switch):
    state = healthy_state()
    state.daily_pnl_usd = -45.0
    decision = risk_engine.evaluate_trade(make_instr(), state)
    assert not decision.approved
    assert decision.rule == "max_daily_loss"
    assert kill_switch.is_active()


def test_emergency_stop_loss_breaker(risk_engine):
    state = healthy_state()
    state.total_pnl_usd = -100.0
    decision = risk_engine.check_circuit_breakers(state)
    assert not decision.approved
    assert decision.rule == "emergency_stop_loss"


def test_drawdown_breaker(risk_engine):
    state = healthy_state()
    state.peak_equity_usd = 1000.0
    state.equity_usd = 700.0  # 30% drawdown > 25% limit
    decision = risk_engine.check_circuit_breakers(state)
    assert not decision.approved
    assert decision.rule == "max_drawdown"


# --- mode gating ---------------------------------------------------------

def test_development_mode_blocks_trading(db):
    settings = make_settings(mode=Mode.DEVELOPMENT)
    engine = RiskEngine(settings, db, KillSwitch(db))
    allowed, problems = engine.trading_allowed()
    assert not allowed


def test_live_mode_requires_confirmation(db):
    settings = make_settings(mode=Mode.LIVE, live_trading_confirm="")
    engine = RiskEngine(settings, db, KillSwitch(db))
    allowed, problems = engine.trading_allowed()
    assert not allowed
    assert any("not confirmed" in p for p in problems)


def test_invalid_instruction_rejected_by_pydantic():
    with pytest.raises(Exception):
        make_instr(side="steal")
    with pytest.raises(Exception):
        make_instr(amount_usd=-5)
