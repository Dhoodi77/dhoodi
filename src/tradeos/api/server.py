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
                "smartmoney": state.db.kv_get("loop_ok_smartmoney"),
            },
            "smartmoney_scanner": bool(state.scanners),
            "smartmoney_chains": [s.chain for s in (state.scanners or [])],
            "smartmoney_last_scan": state.db.kv_get("smartmoney_last_scan"),
            "webhooks_active": bool(state.db.kv_get("helius_webhook_synced_at")),
            "webhook_last_event": state.db.kv_get("helius_webhook_last_event"),
            "provider_health": json.loads(
                state.db.kv_get("provider_health") or "{}"),
        }

    @api.get("/api/desk", dependencies=[Depends(check_auth)])
    async def desk():
        """Everything the desk view needs in one round trip. All values come
        from the database — nothing here is simulated."""
        acct = state.accounting
        allowed, problems = state.risk_engine.trading_allowed()
        started = float(state.db.kv_get("last_startup", "0") or 0)

        agent_rows = state.db.query(
            "SELECT agent, COUNT(*) AS decisions, "
            "SUM(CASE WHEN verdict IN ('buy','approve') THEN 1 ELSE 0 END) AS bullish, "
            "SUM(CASE WHEN verdict = 'reject' THEN 1 ELSE 0 END) AS rejects, "
            "MAX(created_at) AS last_decision_at FROM agent_decisions GROUP BY agent")
        stats_by_agent = {r["agent"]: r for r in agent_rows}
        last_verdicts = {r["agent"]: r for r in state.db.query(
            "SELECT agent, verdict, reasoning, created_at FROM agent_decisions "
            "WHERE id IN (SELECT MAX(id) FROM agent_decisions GROUP BY agent)")}
        agents = []
        for name in state.agents:
            hb_raw = state.db.kv_get(f"agent_hb_{name}")
            hb = json.loads(hb_raw) if hb_raw else None
            stat = stats_by_agent.get(name, {})
            last = last_verdicts.get(name, {})
            agents.append({
                "name": name,
                "heartbeat_at": (hb or {}).get("ts"),
                "task": (hb or {}).get("task", ""),
                "decisions": stat.get("decisions", 0),
                "bullish": stat.get("bullish", 0),
                "rejects": stat.get("rejects", 0),
                "last_verdict": last.get("verdict"),
                "last_reasoning": (last.get("reasoning") or "")[:140],
                "last_decision_at": last.get("created_at"),
            })

        pipeline_counts = {r["status"]: r["n"] for r in state.db.query(
            "SELECT status, COUNT(*) AS n FROM opportunities GROUP BY status")}
        risk_counts = {r["kind"]: r["n"] for r in state.db.query(
            "SELECT kind, COUNT(*) AS n FROM risk_events GROUP BY kind")}
        total_decisions = sum(a["decisions"] for a in agents)

        activity = state.db.query(
            "SELECT actor, action, opportunity_id, created_at FROM audit_log "
            "ORDER BY id DESC LIMIT 40")
        positions = state.db.query(
            "SELECT * FROM positions WHERE status = 'open' ORDER BY opened_at DESC")
        for p in positions:
            last_price = p["last_price_usd"] or p["entry_price_usd"]
            p["unrealized_pnl_usd"] = round(
                (last_price - p["entry_price_usd"]) * p["quantity"], 4)
        closed = state.db.query(
            "SELECT symbol, chain, exit_reason, realized_pnl_usd, closed_at "
            "FROM positions WHERE status = 'closed' ORDER BY closed_at DESC LIMIT 8")
        opportunities = state.db.query(
            "SELECT id, chain, symbol, token_address, status, momentum_score, "
            "smart_money_score, whale_score, risk_score, overall_score, created_at "
            "FROM opportunities ORDER BY created_at DESC LIMIT 20")
        alerts = state.db.query(
            "SELECT priority, title, body, created_at FROM alerts "
            "ORDER BY created_at DESC LIMIT 12")
        starting = settings.paper_starting_balance_usd

        return {
            "mode": settings.mode.value,
            "kill_switch": state.kill_switch.is_active(),
            "trading_allowed": allowed,
            "trading_problems": problems,
            "llm_available": state.llm.available,
            "uptime_s": (time.time() - started) if started else None,
            "equity_usd": round(acct.equity_usd(), 2),
            "cash_usd": round(acct.cash_usd(), 2),
            "exposure_usd": round(acct.exposure_usd(), 2),
            "daily_pnl_usd": round(acct.daily_pnl_usd(), 2),
            "total_pnl_usd": round(acct.realized_pnl_usd()
                                   + acct.unrealized_pnl_usd(), 2),
            "starting_balance_usd": starting,
            "equity_history": acct.equity_history(hours=48),
            "agents": agents,
            "pipeline": {
                "opportunities": pipeline_counts,
                "risk_events": risk_counts,
                "total_agent_decisions": total_decisions,
            },
            "activity": activity,
            "positions": positions,
            "closed_positions": closed,
            "opportunities": opportunities,
            "alerts": alerts,
            "smartmoney_chains": [s.chain for s in (state.scanners or [])],
            "webhooks_active": bool(state.db.kv_get("helius_webhook_synced_at")),
            "provider_health": json.loads(
                state.db.kv_get("provider_health") or "{}"),
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

    @api.get("/api/signals/performance", dependencies=[Depends(check_auth)])
    async def signals_performance():
        from tradeos.learning.post_trade import signal_performance
        return signal_performance(state.db)

    # --- wallet registry (addresses only; keys never enter this system) ---
    @api.get("/api/wallets", dependencies=[Depends(check_auth)])
    async def wallets_list(chain: str | None = None):
        return state.registry.list(chain)

    @api.post("/api/wallets", dependencies=[Depends(check_auth)])
    async def wallets_register(request: Request):
        body = await request.json()
        try:
            wallet_id = state.registry.register(
                label=str(body.get("label", ""))[:100],
                chain=str(body.get("chain", "")),
                address=str(body.get("address", ""))[:100],
                kind=str(body.get("kind", "trading")),
                strategy=body.get("strategy"),
                risk_class=body.get("risk_class"))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"wallet_id": wallet_id}

    @api.post("/api/wallets/{wallet_id}/active", dependencies=[Depends(check_auth)])
    async def wallets_active(wallet_id: int, request: Request):
        body = await request.json()
        if state.registry.get(wallet_id) is None:
            raise HTTPException(404, "no such wallet")
        state.registry.set_active(wallet_id, bool(body.get("active", True)))
        return {"ok": True}

    @api.get("/api/live/readiness", dependencies=[Depends(check_auth)])
    async def live_readiness(chain: str = "solana"):
        """Exactly what stands between the current config and live trading
        on the given chain. Live stays disabled while 'problems' is
        non-empty."""
        if state.live_engine is None:
            return {"ready": False, "problems": ["live engine not wired"]}
        report = await state.live_engine.readiness_full(chain.lower())
        allowed, gate_problems = state.risk_engine.trading_allowed()
        if not allowed:
            report["ready"] = False
            report["problems"] = gate_problems + report["problems"]
        return report

    @api.get("/api/health")
    async def health():
        # Unauthenticated liveness endpoint (no sensitive data).
        return {"ok": True, "ts": time.time()}

    # --- demo mode endpoints -----------------------------------------------
    @api.get("/api/demo/status", dependencies=[Depends(check_auth)])
    async def demo_status():
        """Demo mode status and safety information."""
        return {
            "demo_mode": settings.demo_mode,
            "trading_mode": settings.mode.value,
            "starting_balance": settings.demo_starting_balance,
            "paper_only": True if settings.demo_mode else False,
            "live_trading_disabled": True if settings.demo_mode else settings.live_trading_confirm == "",
            "safety_message": "🟢 DEMO / PAPER TRADING — No real transactions possible",
            "is_safe_demo": settings.demo_mode and settings.mode == Mode.PAPER,
        }

    @api.post("/api/demo/reset", dependencies=[Depends(check_auth)])
    async def demo_reset():
        """Reset paper trading state (demo mode only). WARNING: Destructive."""
        if not settings.demo_mode:
            raise HTTPException(400, "reset only available in demo mode")
        # Drop and recreate paper trading data
        state.accounting.reset_paper_trades()
        state.db.system_event("demo_reset", "user reset paper trading")
        return {"reset": True, "balance": state.accounting.equity_usd()}

    @api.post("/api/demo/agents/start", dependencies=[Depends(check_auth)])
    async def demo_agents_start():
        """Start all agents (demo)."""
        if not state.scheduler.running:
            await state.scheduler.start()
        return {"agents_running": True}

    @api.post("/api/demo/agents/pause", dependencies=[Depends(check_auth)])
    async def demo_agents_pause():
        """Pause all agents (demo)."""
        if state.scheduler.running:
            await state.scheduler.stop()
        return {"agents_paused": True}

    @api.get("/api/demo/agents/status", dependencies=[Depends(check_auth)])
    async def demo_agents_status():
        """Get agent statuses (demo)."""
        agents_status = {}
        for name in state.agents:
            hb_raw = state.db.kv_get(f"agent_hb_{name}")
            hb = json.loads(hb_raw) if hb_raw else None
            agents_status[name] = {
                "name": name,
                "online": hb is not None,
                "last_heartbeat": (hb or {}).get("ts"),
                "task": (hb or {}).get("task", ""),
            }
        return {"agents": agents_status, "scheduler_running": state.scheduler.running}

    # --- Helius webhook receiver --------------------------------------
    # Authenticated by the shared secret Helius echoes verbatim in the
    # Authorization header (configured at webhook registration) — the
    # dashboard token is never given to a third party.
    @api.post("/webhooks/helius")
    async def helius_webhook(request: Request):
        secret = settings.helius_webhook_secret
        if not secret or state.webhook_manager is None:
            raise HTTPException(404, "webhooks not configured")
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied, secret):
            raise HTTPException(401, "unauthorized")
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json")
        if not isinstance(payload, list):
            raise HTTPException(400, "expected a list of transactions")
        return await state.webhook_manager.handle_payload(payload[:100])

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
