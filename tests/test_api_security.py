"""API auth, kill-switch endpoint, secret redaction, live-engine refusal."""
import logging

import pytest
from fastapi.testclient import TestClient

from tradeos.api.server import create_server
from tradeos.app import build_app
from tradeos.config import Mode
from tradeos.execution.instructions import TradeInstruction
from tradeos.execution.live import LiveExecutionEngine
from tradeos.logging_setup import JsonFormatter, redact

from .conftest import make_settings


@pytest.fixture
def client(monkeypatch):
    settings = make_settings()
    app_state = build_app(settings)
    # keep the scheduler quiet during API tests
    server = create_server(app_state)
    with TestClient(server, raise_server_exceptions=False) as c:
        yield c, app_state
    app_state.db.close()


def test_unauthenticated_requests_rejected(client):
    c, _ = client
    for path in ("/api/dashboard", "/api/positions", "/api/audit"):
        assert c.get(path).status_code == 401
    assert c.post("/api/kill-switch/activate").status_code == 401


def test_wrong_token_rejected(client):
    c, _ = client
    r = c.get("/api/dashboard", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_valid_token_accepted(client):
    c, _ = client
    r = c.get("/api/dashboard", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "paper"
    assert body["equity_usd"] == 500.0


def test_health_endpoint_is_public_and_minimal(client):
    c, _ = client
    r = c.get("/api/health")
    assert r.status_code == 200
    assert set(r.json().keys()) == {"ok", "ts"}


def test_kill_switch_via_api(client):
    c, state = client
    headers = {"Authorization": "Bearer test-token"}
    r = c.post("/api/kill-switch/activate", headers=headers,
               json={"reason": "api test"})
    assert r.status_code == 200
    assert state.kill_switch.is_active()
    dash = c.get("/api/dashboard", headers=headers).json()
    assert dash["kill_switch"] is True
    r2 = c.post("/api/kill-switch/resume", headers=headers)
    assert r2.status_code == 200
    assert not state.kill_switch.is_active()


def test_missing_dashboard_token_refuses_start():
    settings = make_settings(dashboard_token=None, mode=Mode.PAPER)
    app_state = build_app(settings)
    with pytest.raises(RuntimeError):
        create_server(app_state)
    app_state.db.close()


def test_repeated_bad_tokens_rate_limited(client):
    c, _ = client
    for _ in range(10):
        c.get("/api/dashboard", headers={"Authorization": "Bearer nope"})
    r = c.get("/api/dashboard", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 429


# --- secret redaction ----------------------------------------------------

def test_redaction_scrubs_key_shapes():
    assert "sk-ant" not in redact("key is sk-ant-abc123def456ghi789jkl")
    assert "deadbeef" not in redact("pk " + "deadbeef" * 8)
    assert "[REDACTED]" in redact("Bearer abcdefghijklmnopqrstuv")


def test_log_formatter_redacts(caplog):
    formatter = JsonFormatter()
    record = logging.LogRecord("t", logging.INFO, __file__, 1,
                               "leaked sk-ant-abc123def456ghi789jkl", (), None)
    assert "sk-ant" not in formatter.format(record)


# --- live engine ---------------------------------------------------------

def test_live_engine_fails_closed(db):
    settings = make_settings(mode=Mode.LIVE,
                             live_trading_confirm="I_UNDERSTAND_THE_RISKS")
    engine = LiveExecutionEngine(settings, db)
    result = engine.execute(TradeInstruction(
        opportunity_id="opp_live", chain="solana", token_address="t",
        side="buy", amount_usd=10, max_slippage_pct=1, max_gas_usd=1), 1.0)
    assert not result.ok
    assert "not implemented" in result.error
    trades = db.query("SELECT * FROM trades WHERE mode = 'live'")
    assert trades[0]["status"] == "failed"
