"""FastAPI server: mobile-first dashboard + control API.

Authentication: static bearer token (TRADEOS_DASHBOARD_TOKEN) compared in
constant time, with simple in-memory rate limiting on failures. The server
refuses to start without a token unless in development mode. The kill
switch endpoint calls straight into the deterministic KillSwitch — no AI in
that path.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import defaultdict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from tradeos.app import App, build_app
from tradeos.config import Mode

STATIC_DIR = Path(__file__).parent.parent / "web" / "static"

_failures: dict[str, list[float]] = defaultdict(list)
_bearer = HTTPBearer(auto_error=False)


def create_server(app_state: App | None = None) -> FastAPI:
    state = app_state or build_app()
    settings = state.settings

    if not settings.dashboard_token and settings.mode != Mode.DEVELOPMENT:
        raise RuntimeError("TRADEOS_DASHBOARD_TOKEN is required outside development mode")

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await state.scheduler.start()
        try:
            yield
        finally:
            await state.scheduler.stop()
            await state.market.close()

    api = FastAPI(title="TradeOS", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    api.state.tradeos = state

    def check_auth(request: Request,
                   creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
        if settings.mode == Mode.DEVELOPMENT and not settings.dashboard_token:
            return
        client = request.client.host if request.client else "unknown"
        now = time.time()
        _failures[client] = [t for t in _failures[client] if now - t < 300]
        if len(_failures[client]) >= 10:
            raise HTTPException(429, "too many failed attempts")
        token = creds.credentials if creds else request.query_params.get("token", "")
        if not token or not secrets.compare_digest(token, settings.dashboard_token or ""):
            _failures[client].append(now)
            raise HTTPException(401, "unauthorized")

    # --- UI -----------------------------------------------------------
    @api.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    # --- read API -----------------------------------------------------
    @api.get("/api/dashboard", dependencies=[Depends(check_auth)])
    async def dashboard():
        acct = state.accounting
        allowed, problems = state.risk_engine.trading_allowed()
        hb = {}
        for name in state.agents:
            raw = state.db.kv_get(f"agent_hb_{name}")
            hb[name] = json.loads(raw) if raw else None
        return {
            "mode": settings.mode.value,
            "kill_switch": state.kill_switch.is_active(),
            "kill_switch_reason": state.kill_switch.reason(),
            "trading_allowed": allowed,
            "trading_problems": problems,
            "equity_usd": round(acct.equity_usd(), 2),
            "cash_usd": round(acct.cash_usd(), 2),
            "exposure_usd": round(acct.exposure_usd(), 2),
            "daily_pnl_usd": round(acct.daily_pnl_usd(), 2),
            "total_realized_pnl_usd": round(acct.realized_pnl_usd(), 2),
            "unrealized_pnl_usd": round(acct.unrealized_pnl_usd(), 2),
            "open_positions": len(acct.open_positions()),
            "agents": hb,
            "llm_available": state.llm.available,
            "loops": {
                "discovery": state.db.kv_get("loop_ok_discovery"),
                "monitor": state.db.kv_get("loop_ok_monitor"),
            },
        }

    @api.get("/api/positions", dependencies=[Depends(check_auth)])
    async def positions(status: str = "open", limit: int = 50):
        if status not in ("open", "closed"):
            raise HTTPException(400, "status must be open|closed")
        rows = state.db.query(
            "SELECT * FROM positions WHERE status = ? ORDER BY opened_at DESC LIMIT ?",
            (status, min(limit, 200)))
        for r in rows:
            last = r["last_price_usd"] or r["entry_price_usd"]
            r["unrealized_pnl_usd"] = round(
                (last - r["entry_price_usd"]) * r["quantity"], 4)
        return rows

    @api.get("/api/opportunities", dependencies=[Depends(check_auth)])
    async def opportunities(limit: int = 30):
        return state.db.query(
            "SELECT id, chain, token_address, symbol, status, momentum_score, "
            "smart_money_score, risk_score, overall_score, created_at "
            "FROM opportunities ORDER BY created_at DESC LIMIT ?", (min(limit, 200),))

    @api.get("/api/opportunities/{opp_id}", dependencies=[Depends(check_auth)])
    async def opportunity_detail(opp_id: str):
        opp = state.db.query_one("SELECT * FROM opportunities WHERE id = ?", (opp_id,))
        if opp is None:
            raise HTTPException(404, "not found")
        return {
            "opportunity": opp,
            "agent_decisions": state.db.query(
                "SELECT agent, verdict, confidence, reasoning, evidence_json, model_used, "
                "created_at FROM agent_decisions WHERE opportunity_id = ? ORDER BY id",
                (opp_id,)),
            "risk_events": state.db.query(
                "SELECT kind, rule, detail, created_at FROM risk_events "
                "WHERE opportunity_id = ? ORDER BY id", (opp_id,)),
            "trades": state.db.query(
                "SELECT * FROM trades WHERE opportunity_id = ? ORDER BY id", (opp_id,)),
            "research": state.db.query(
                "SELECT * FROM research_reports WHERE opportunity_id = ?", (opp_id,)),
        }

    @api.get("/api/trades", dependencies=[Depends(check_auth)])
    async def trades(limit: int = 50):
        return state.db.query(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (min(limit, 200),))

    @api.get("/api/alerts", dependencies=[Depends(check_auth)])
    async def alerts(limit: int = 50):
        return state.db.query(
            "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (min(limit, 200),))

    @api.get("/api/audit", dependencies=[Depends(check_auth)])
    async def audit(limit: int = 100, opportunity_id: str | None = None):
        if opportunity_id:
            return state.db.query(
                "SELECT * FROM audit_log WHERE opportunity_id = ? ORDER BY id DESC LIMIT ?",
                (opportunity_id, min(limit, 500)))
        return state.db.query(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (min(limit, 500),))

    @api.get("/api/reviews", dependencies=[Depends(check_auth)])
    async def reviews(limit: int = 20):
        return state.db.query(
            "SELECT * FROM post_trade_reviews ORDER BY created_at DESC LIMIT ?",
            (min(limit, 100),))

    @api.get("/api/wallets/smart-money", dependencies=[Depends(check_auth)])
    async def smart_money():
        return state.reputation.tracked_smart_money()

    @api.get("/api/health")
    async def health():
        # Unauthenticated liveness endpoint (no sensitive data).
        return {"ok": True, "ts": time.time()}

    # --- control API --------------------------------------------------
    @api.post("/api/kill-switch/activate", dependencies=[Depends(check_auth)])
    async def kill_activate(request: Request):
        body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
        reason = str(body.get("reason", "user request"))[:500]
        state.kill_switch.activate("user", reason)
        return {"kill_switch": True}

    @api.post("/api/kill-switch/resume", dependencies=[Depends(check_auth)])
    async def kill_resume():
        # Resume requires passing safety checks first.
        problems: list[str] = []
        if state.risk_engine.policy is None:
            problems += state.risk_engine.policy_errors
        else:
            breaker = state.risk_engine.check_circuit_breakers(state.accounting.state())
            if not breaker.approved:
                problems += breaker.reasons
        if problems:
            return JSONResponse(status_code=409, content={
                "resumed": False, "problems": problems,
                "note": "safety checks failed; kill switch stays active"})
        try:
            state.kill_switch.deactivate("user")
        except RuntimeError as exc:
            return JSONResponse(status_code=409,
                                content={"resumed": False, "problems": [str(exc)]})
        return {"resumed": True}

    @api.post("/api/positions/{position_id}/close", dependencies=[Depends(check_auth)])
    async def close_position(position_id: int):
        pos = state.db.query_one(
            "SELECT * FROM positions WHERE id = ? AND status = 'open'", (position_id,))
        if pos is None:
            raise HTTPException(404, "no open position with that id")
        pairs = await state.market.get_token_pairs(pos["chain"], pos["token_address"])
        price = max(pairs, key=lambda p: p.liquidity_usd).price_usd if pairs else None
        if not price:
            raise HTTPException(502, "no market price available to close")
        closed = await state.monitor._close(pos, price, "manual")
        return {"closed": bool(closed)}

    @api.post("/api/analyze", dependencies=[Depends(check_auth)])
    async def analyze_token(request: Request):
        body = await request.json()
        chain = str(body.get("chain", "")).lower()
        token = str(body.get("token_address", ""))
        if not chain or not token:
            raise HTTPException(400, "chain and token_address required")
        pairs = await state.market.get_token_pairs(chain, token)
        if not pairs:
            raise HTTPException(404, "no pairs found for token")
        best = max(pairs, key=lambda p: p.liquidity_usd)
        decision = await state.pipeline.process(best)
        if decision is None:
            return {"processed": False,
                    "note": "token filtered (already active, or chain not allowed)"}
        return {"processed": True, "opportunity_id": decision.opportunity_id,
                "final": decision.final, "rationale": decision.rationale}

    return api
