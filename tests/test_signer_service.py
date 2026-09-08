"""Reference signer service: auth, policy refusals, real ed25519 signing.

Uses solders to build a genuine unsigned Solana transaction and verifies
the signer produces a validly signed one. The signer module lives outside
the tradeos package on purpose (it's a separate process) — imported here
by path.
"""
import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

SIGNER_DIR = Path(__file__).parent.parent / "signer"


def load_signer_module():
    spec = importlib.util.spec_from_file_location(
        "solana_signer", SIGNER_DIR / "solana_signer.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["solana_signer"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def signer_env(tmp_path, monkeypatch):
    keypair = Keypair()
    keyfile = tmp_path / "id.json"
    keyfile.write_text(json.dumps(list(bytes(keypair))))
    monkeypatch.setenv("SIGNER_KEYPAIR_PATH", str(keyfile))
    monkeypatch.setenv("SIGNER_TOKEN", "signer-test-token")
    monkeypatch.setenv("SIGNER_MAX_PER_HOUR", "3")
    module = load_signer_module()
    monkeypatch.setattr(module, "KILL_FILE", tmp_path / "SIGNER_KILLSWITCH")
    client = TestClient(module.create_app())
    return client, keypair, module, tmp_path


def unsigned_tx_b64(fee_payer: Keypair) -> str:
    """A real (unsigned) v0 transaction with the given fee payer."""
    instruction = transfer(TransferParams(
        from_pubkey=fee_payer.pubkey(),
        to_pubkey=Pubkey.new_unique(), lamports=1))
    message = MessageV0.try_compile(
        payer=fee_payer.pubkey(), instructions=[instruction],
        address_lookup_table_accounts=[], recent_blockhash=Hash.default())
    tx = VersionedTransaction.populate(
        message, [Signature.default()] * message.header.num_required_signatures)
    return base64.b64encode(bytes(tx)).decode()


AUTH = {"Authorization": "Bearer signer-test-token"}


def test_health_reports_fee_payer(signer_env):
    client, keypair, _, _ = signer_env
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["fee_payer"] == str(keypair.pubkey())


def test_auth_required(signer_env):
    client, keypair, _, _ = signer_env
    tx = unsigned_tx_b64(keypair)
    assert client.post("/sign", json={"transaction": tx}).status_code == 401
    assert client.post("/sign", json={"transaction": tx},
                       headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_signs_own_fee_payer_and_signature_verifies(signer_env):
    client, keypair, _, _ = signer_env
    tx = unsigned_tx_b64(keypair)
    resp = client.post("/sign", json={"transaction": tx, "intent": {"t": 1}},
                       headers=AUTH)
    assert resp.status_code == 200, resp.text
    signed = VersionedTransaction.from_bytes(
        base64.b64decode(resp.json()["signed_transaction"]))
    assert signed.verify_and_hash_message() is not None  # signature valid


def test_refuses_foreign_fee_payer(signer_env):
    client, _, _, _ = signer_env
    other = Keypair()
    tx = unsigned_tx_b64(other)  # fee payer is NOT the signer's key
    resp = client.post("/sign", json={"transaction": tx}, headers=AUTH)
    assert resp.status_code == 403
    assert "fee payer" in resp.json()["detail"]


def test_rate_limit(signer_env):
    client, keypair, _, _ = signer_env
    tx = unsigned_tx_b64(keypair)
    for _ in range(3):
        assert client.post("/sign", json={"transaction": tx},
                           headers=AUTH).status_code == 200
    resp = client.post("/sign", json={"transaction": tx}, headers=AUTH)
    assert resp.status_code == 403
    assert "rate limit" in resp.json()["detail"]


def test_kill_file_stops_signing(signer_env):
    client, keypair, module, tmp_path = signer_env
    (tmp_path / "SIGNER_KILLSWITCH").touch()
    tx = unsigned_tx_b64(keypair)
    resp = client.post("/sign", json={"transaction": tx}, headers=AUTH)
    assert resp.status_code == 403
    assert "kill switch" in resp.json()["detail"]


def test_unparseable_transaction_rejected(signer_env):
    client, _, _, _ = signer_env
    resp = client.post("/sign", json={"transaction": "bm90IGEgdHg="},
                       headers=AUTH)
    assert resp.status_code == 400
