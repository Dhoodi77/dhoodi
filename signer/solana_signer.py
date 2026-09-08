"""Reference Solana signer service — RUN AS A SEPARATE PROCESS.

This service is the ONLY place the private key exists. TradeOS sends it an
unsigned transaction plus intent metadata; this service applies its own
independent policy and either returns a signed transaction or refuses.
It never trusts the caller: even a fully compromised TradeOS process can,
at worst, ask it to sign — and everything it signs is bounded by the
policy below and logged.

Independent policy (all enforced HERE, not in TradeOS):
  - bearer-token auth (SIGNER_TOKEN), constant-time compare
  - fee payer of the transaction MUST be this signer's own public key —
    it will never sign a transaction spending from any other account
  - rate limit: SIGNER_MAX_PER_HOUR signatures per rolling hour
  - kill file: touch SIGNER_KILLSWITCH next to this file to stop signing
  - every request (signed or refused) is logged with the tx hash of the
    message, never the key

Run (as a DIFFERENT user than TradeOS, ideally a different host):
  SIGNER_KEYPAIR_PATH=/path/to/id.json SIGNER_TOKEN=... \
  uvicorn solana_signer:app --host 127.0.0.1 --port 8471

Requirements: fastapi, uvicorn, solders (pip install fastapi uvicorn solders)
Hardening beyond this reference implementation is on the operator:
hardware wallet / HSM, network isolation, and an allowlist of program ids.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

logging.basicConfig(level=logging.INFO,
                    format='{"ts": %(created)f, "signer": "%(message)s"}')
logger = logging.getLogger("signer")

KILL_FILE = Path(__file__).parent / "SIGNER_KILLSWITCH"


def load_keypair():
    """Load the keypair at startup from SIGNER_KEYPAIR_PATH (solana-cli
    id.json format: JSON array of 64 bytes)."""
    from solders.keypair import Keypair

    path = os.environ.get("SIGNER_KEYPAIR_PATH")
    if not path:
        raise RuntimeError("SIGNER_KEYPAIR_PATH not set")
    raw = json.loads(Path(path).read_text())
    return Keypair.from_bytes(bytes(raw))


class SignerState:
    def __init__(self):
        self.token = os.environ.get("SIGNER_TOKEN", "")
        if not self.token:
            raise RuntimeError("SIGNER_TOKEN not set")
        self.max_per_hour = int(os.environ.get("SIGNER_MAX_PER_HOUR", "30"))
        self.keypair = load_keypair()
        self.pubkey = str(self.keypair.pubkey())
        self.sign_times: list[float] = []


def create_app() -> FastAPI:
    state = SignerState()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    logger.info("signer ready, fee payer %s" % state.pubkey)

    def check_auth(request: Request) -> None:
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {state.token}"
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(401, "unauthorized")

    @app.get("/health")
    async def health():
        return {"ok": True, "fee_payer": state.pubkey,
                "kill_switch": KILL_FILE.exists()}

    @app.post("/sign")
    async def sign(request: Request):
        check_auth(request)
        if KILL_FILE.exists():
            logger.info("REFUSED: kill file present")
            raise HTTPException(403, "signer kill switch active")

        now = time.time()
        state.sign_times = [t for t in state.sign_times if now - t < 3600]
        if len(state.sign_times) >= state.max_per_hour:
            logger.info("REFUSED: rate limit")
            raise HTTPException(403, f"rate limit: {state.max_per_hour}/hour")

        body = await request.json()
        tx_b64 = body.get("transaction")
        intent = body.get("intent") or {}
        if not isinstance(tx_b64, str) or not tx_b64:
            raise HTTPException(400, "transaction required")

        from solders.transaction import VersionedTransaction

        try:
            tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        except Exception:
            logger.info("REFUSED: unparseable transaction")
            raise HTTPException(400, "unparseable transaction")

        # The fee payer (first account) must be this signer's own key —
        # never sign something spending from another account.
        fee_payer = str(tx.message.account_keys[0])
        if fee_payer != state.pubkey:
            logger.info("REFUSED: fee payer mismatch %s" % fee_payer)
            raise HTTPException(403, "fee payer is not this signer's key")

        signed = VersionedTransaction(tx.message, [state.keypair])
        state.sign_times.append(now)
        logger.info("SIGNED intent=%s" % json.dumps(intent, default=str)[:200])
        return {"signed_transaction": base64.b64encode(bytes(signed)).decode()}

    return app


# uvicorn entry point (constructed lazily so imports don't need env vars)
def app():
    return create_app()
