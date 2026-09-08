"""Reference signer service — RUN AS A SEPARATE PROCESS.

This service is the ONLY place private keys exist. TradeOS sends it an
unsigned transaction plus intent metadata; this service applies its own
independent policy and either returns a signed transaction or refuses.
It never trusts the caller: even a fully compromised TradeOS process can,
at worst, ask it to sign — and everything it signs is bounded by the
policy below and logged.

Two independent key slots, each enabled only when its env var is set:

  Solana  SIGNER_KEYPAIR_PATH      (solana-cli id.json)   -> POST /sign
  EVM     SIGNER_EVM_KEY_PATH      (file with hex key)    -> POST /sign-evm

Shared policy (enforced HERE, not in TradeOS):
  - bearer-token auth (SIGNER_TOKEN), constant-time compare
  - rolling rate limit (SIGNER_MAX_PER_HOUR, per key slot)
  - kill file: touch SIGNER_KILLSWITCH next to this file to stop signing
  - every request (signed or refused) is logged; keys are never logged

Solana-specific policy:
  - the transaction's fee payer MUST be the signer's own public key

EVM-specific policy:
  - native value capped at SIGNER_EVM_MAX_VALUE_WEI (default 0.1 native)
  - chainId must be in SIGNER_EVM_CHAIN_IDS (default 1,8453,56,42161,137)
  - optional SIGNER_EVM_TO_ALLOWLIST (comma-separated addresses): when
    set, only transactions to those contracts are signed

Run (as a DIFFERENT user than TradeOS, ideally a different host):
  SIGNER_TOKEN=... SIGNER_KEYPAIR_PATH=... SIGNER_EVM_KEY_PATH=... \
  uvicorn 'service:app' --factory --host 127.0.0.1 --port 8471

Requirements: fastapi, uvicorn, solders (Solana), eth-account (EVM).
Hardening beyond this reference implementation is on the operator:
hardware wallet / HSM, network isolation, program-id allowlists.
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

DEFAULT_EVM_CHAIN_IDS = "1,8453,56,42161,137"
DEFAULT_EVM_MAX_VALUE_WEI = 100_000_000_000_000_000  # 0.1 native


def load_solana_keypair():
    from solders.keypair import Keypair

    path = os.environ.get("SIGNER_KEYPAIR_PATH")
    if not path:
        return None
    raw = json.loads(Path(path).read_text())
    return Keypair.from_bytes(bytes(raw))


def load_evm_account():
    from eth_account import Account

    path = os.environ.get("SIGNER_EVM_KEY_PATH")
    if not path:
        return None
    key_hex = Path(path).read_text().strip()
    return Account.from_key(key_hex)


class SignerState:
    def __init__(self):
        self.token = os.environ.get("SIGNER_TOKEN", "")
        if not self.token:
            raise RuntimeError("SIGNER_TOKEN not set")
        self.max_per_hour = int(os.environ.get("SIGNER_MAX_PER_HOUR", "30"))
        self.solana_keypair = load_solana_keypair()
        self.evm_account = load_evm_account()
        if self.solana_keypair is None and self.evm_account is None:
            raise RuntimeError("no key configured: set SIGNER_KEYPAIR_PATH "
                               "and/or SIGNER_EVM_KEY_PATH")
        self.solana_pubkey = str(self.solana_keypair.pubkey()) \
            if self.solana_keypair else None
        self.evm_address = self.evm_account.address.lower() \
            if self.evm_account else None
        self.evm_max_value_wei = int(os.environ.get(
            "SIGNER_EVM_MAX_VALUE_WEI", DEFAULT_EVM_MAX_VALUE_WEI))
        self.evm_chain_ids = {
            int(c) for c in os.environ.get(
                "SIGNER_EVM_CHAIN_IDS", DEFAULT_EVM_CHAIN_IDS).split(",") if c}
        allow = os.environ.get("SIGNER_EVM_TO_ALLOWLIST", "")
        self.evm_to_allowlist = {a.strip().lower() for a in allow.split(",")
                                 if a.strip()}
        self.sign_times: dict[str, list[float]] = {"solana": [], "evm": []}


def create_app() -> FastAPI:
    state = SignerState()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    logger.info("signer ready solana=%s evm=%s"
                % (state.solana_pubkey, state.evm_address))

    def check_auth(request: Request) -> None:
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {state.token}"
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(401, "unauthorized")

    def check_shared_policy(slot: str) -> None:
        if KILL_FILE.exists():
            logger.info("REFUSED: kill file present")
            raise HTTPException(403, "signer kill switch active")
        now = time.time()
        times = [t for t in state.sign_times[slot] if now - t < 3600]
        state.sign_times[slot] = times
        if len(times) >= state.max_per_hour:
            logger.info("REFUSED: rate limit slot=%s" % slot)
            raise HTTPException(403, f"rate limit: {state.max_per_hour}/hour")

    @app.get("/health")
    async def health():
        return {"ok": True, "solana_fee_payer": state.solana_pubkey,
                "evm_address": state.evm_address,
                "kill_switch": KILL_FILE.exists()}

    # --- Solana --------------------------------------------------------
    @app.post("/sign")
    async def sign_solana(request: Request):
        check_auth(request)
        if state.solana_keypair is None:
            raise HTTPException(404, "no solana key configured")
        check_shared_policy("solana")

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
        if fee_payer != state.solana_pubkey:
            logger.info("REFUSED: fee payer mismatch %s" % fee_payer)
            raise HTTPException(403, "fee payer is not this signer's key")

        signed = VersionedTransaction(tx.message, [state.solana_keypair])
        state.sign_times["solana"].append(time.time())
        logger.info("SIGNED solana intent=%s"
                    % json.dumps(intent, default=str)[:200])
        return {"signed_transaction": base64.b64encode(bytes(signed)).decode()}

    # --- EVM -----------------------------------------------------------
    @app.post("/sign-evm")
    async def sign_evm(request: Request):
        check_auth(request)
        if state.evm_account is None:
            raise HTTPException(404, "no evm key configured")
        check_shared_policy("evm")

        body = await request.json()
        tx = body.get("transaction")
        intent = body.get("intent") or {}
        if not isinstance(tx, dict):
            raise HTTPException(400, "transaction object required")

        required = ("chainId", "nonce", "to", "value", "data", "gas",
                    "maxFeePerGas", "maxPriorityFeePerGas")
        if any(field not in tx for field in required):
            raise HTTPException(400, f"transaction must carry {required}")
        try:
            chain_id = int(tx["chainId"])
            value = int(tx["value"])
            to = str(tx["to"]).lower()
        except (TypeError, ValueError):
            raise HTTPException(400, "unparseable transaction fields")

        if chain_id not in state.evm_chain_ids:
            logger.info("REFUSED: chainId %s not allowed" % chain_id)
            raise HTTPException(403, f"chainId {chain_id} not allowed")
        if value > state.evm_max_value_wei:
            logger.info("REFUSED: value %s over cap" % value)
            raise HTTPException(403, "native value exceeds signer cap")
        if state.evm_to_allowlist and to not in state.evm_to_allowlist:
            logger.info("REFUSED: to %s not in allowlist" % to)
            raise HTTPException(403, "destination not in signer allowlist")

        try:
            signed = state.evm_account.sign_transaction({
                "chainId": chain_id, "nonce": int(tx["nonce"]), "to": tx["to"],
                "value": value, "data": tx["data"], "gas": int(tx["gas"]),
                "maxFeePerGas": int(tx["maxFeePerGas"]),
                "maxPriorityFeePerGas": int(tx["maxPriorityFeePerGas"])})
        except (ValueError, TypeError) as exc:
            logger.info("REFUSED: unsignable %s" % type(exc).__name__)
            raise HTTPException(400, "transaction not signable")

        state.sign_times["evm"].append(time.time())
        logger.info("SIGNED evm chain=%s to=%s intent=%s"
                    % (chain_id, to, json.dumps(intent, default=str)[:200]))
        return {"raw_transaction": signed.raw_transaction.hex()}

    return app


# uvicorn entry point (constructed lazily so imports don't need env vars)
def app():
    return create_app()
