"""Paper execution, portfolio accounting, and P&L."""
from tradeos.execution.instructions import TradeInstruction


def buy_instr(amount=20.0, **kw) -> TradeInstruction:
    d = dict(opportunity_id="opp_p", chain="solana", token_address="TokP",
             symbol="TOKP", side="buy", amount_usd=amount,
             max_slippage_pct=2.0, max_gas_usd=1.0)
    d.update(kw)
    return TradeInstruction(**d)


def test_buy_opens_position_and_debits_cash(gateway, accounting):
    result = gateway.submit(buy_instr(), market_price_usd=0.5)
    assert result.ok
    assert accounting.cash_usd() == 480.0
    positions = accounting.open_positions()
    assert len(positions) == 1
    pos = positions[0]
    # 1% simulated slippage: fill at 0.505
    assert abs(pos["entry_price_usd"] - 0.505) < 1e-9
    assert abs(pos["cost_usd"] - 20.0) < 1e-9
    assert abs(pos["quantity"] - 20.0 / 0.505) < 1e-6


def test_sell_realizes_pnl(gateway, accounting, db):
    result = gateway.submit(buy_instr(), market_price_usd=0.5)
    pos_id = result.position_id
    # price doubles; sell fills at 1.0 * 0.99 = 0.99
    sell = TradeInstruction(
        opportunity_id="opp_p", chain="solana", token_address="TokP",
        symbol="TOKP", side="sell", amount_usd=20.0, max_slippage_pct=2.0,
        max_gas_usd=1.0, position_id=pos_id, reason="take_profit")
    result2 = gateway.submit(sell, market_price_usd=1.0)
    assert result2.ok
    pos = db.query_one("SELECT * FROM positions WHERE id = ?", (pos_id,))
    assert pos["status"] == "closed"
    quantity = 20.0 / 0.505
    expected_pnl = 0.99 * quantity - 20.0
    assert abs(pos["realized_pnl_usd"] - expected_pnl) < 1e-6
    assert accounting.realized_pnl_usd() > 0
    assert abs(accounting.cash_usd() - (480.0 + 0.99 * quantity)) < 1e-6


def test_sell_without_position_fails(gateway):
    sell = TradeInstruction(
        opportunity_id="opp_p", chain="solana", token_address="TokP",
        side="sell", amount_usd=20.0, max_slippage_pct=2.0, max_gas_usd=1.0,
        position_id=99999)
    result = gateway.submit(sell, market_price_usd=1.0)
    assert not result.ok


def test_zero_price_fails_closed(gateway):
    result = gateway.submit(buy_instr(), market_price_usd=0.0)
    assert not result.ok


def test_equity_and_exposure(gateway, accounting):
    gateway.submit(buy_instr(), market_price_usd=0.5)
    assert accounting.exposure_usd() == 20.0
    # no price move recorded yet: unrealized == 0, equity == 500
    assert abs(accounting.equity_usd() - 500.0) < 1e-6


def test_duplicate_execution_bounded_by_exposure(gateway, accounting):
    """Repeated $20 buys stop at the exposure/position limits, never runaway."""
    approved = 0
    for _ in range(10):
        if gateway.submit(buy_instr(), market_price_usd=0.5).ok:
            approved += 1
    # limits: 5 open positions max, $100 exposure max -> exactly 5 fills
    assert approved == 5
    assert accounting.exposure_usd() == 100.0


def test_every_trade_is_audited(gateway, db):
    gateway.submit(buy_instr(), market_price_usd=0.5)
    audit = db.query("SELECT * FROM audit_log WHERE actor = 'execution_paper'")
    assert len(audit) >= 1
    risk_events = db.query("SELECT * FROM risk_events")
    assert len(risk_events) >= 1
