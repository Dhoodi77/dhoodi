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
        "signer_service", SIGNER_DIR / "service.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["signer_service"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def signer_env(tmp_path, monkeypatch):
    keypair = Keypair()
    keyfile = tmp_path / "id.json"
    keyfile.write_text(json.dumps(list(bytes(keypair))))
    monkeypatch.setenv("SIGNER_KEYPAIR_PATH", str(keyfile))
    monkeypatch.delenv("SIGNER_EVM_KEY_PATH", raising=False)
    monkeypatch.setenv("SIGNER_TOKEN", "signer-test-token")
    monkeypatch.setenv("SIGNER_MAX_PER_HOUR", "3")
    module = load_signer_module()
    monkeypatch.setattr(module, "KILL_FILE", tmp_path / "SIGNER_KILLSWITCH")
    client = TestClient(module.create_app())
    return client, keypair, module, tmp_path


@pytest.fixture
def evm_signer_env(tmp_path, monkeypatch):
    from eth_account import Account

    account = Account.create()
    keyfile = tmp_path / "evm.key"
    keyfile.write_text(account.key.hex())
    monkeypatch.delenv("SIGNER_KEYPAIR_PATH", raising=False)
    monkeypatch.setenv("SIGNER_EVM_KEY_PATH", str(keyfile))
    monkeypatch.setenv("SIGNER_TOKEN", "signer-test-token")
    monkeypatch.setenv("SIGNER_MAX_PER_HOUR", "3")
    monkeypatch.setenv("SIGNER_EVM_MAX_VALUE_WEI", str(10**17))
    module = load_signer_module()
    monkeypatch.setattr(module, "KILL_FILE", tmp_path / "SIGNER_KILLSWITCH")
    client = TestClient(module.create_app())
    return client, account, module, tmp_path


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
    assert body["solana_fee_payer"] == str(keypair.pubkey())
    assert body["evm_address"] is None


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


def test_evm_endpoint_404_without_evm_key(signer_env):
    client, _, _, _ = signer_env
    resp = client.post("/sign-evm", json={"transaction": {}}, headers=AUTH)
    assert resp.status_code == 404


# --- EVM slot -------------------------------------------------------------

def evm_tx(value=10**15, chain_id=8453):
    return {"chainId": chain_id, "nonce": 0, "to": "0x" + "11" * 20,
            "value": value, "data": "0x", "gas": 21000,
            "maxFeePerGas": 10**9, "maxPriorityFeePerGas": 10**8}


def test_evm_sign_and_recover(evm_signer_env):
    from eth_account import Account

    client, account, _, _ = evm_signer_env
    resp = client.post("/sign-evm", json={"transaction": evm_tx(),
                                          "intent": {"t": 1}}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    raw = resp.json()["raw_transaction"]
    assert Account.recover_transaction(raw) == account.address


def test_evm_value_cap_enforced(evm_signer_env):
    client, _, _, _ = evm_signer_env
    resp = client.post("/sign-evm", json={"transaction": evm_tx(value=10**18)},
                       headers=AUTH)
    assert resp.status_code == 403
    assert "value" in resp.json()["detail"]


def test_evm_chain_allowlist(evm_signer_env):
    client, _, _, _ = evm_signer_env
    resp = client.post("/sign-evm", json={"transaction": evm_tx(chain_id=999)},
                       headers=AUTH)
    assert resp.status_code == 403


def test_evm_missing_fields_rejected(evm_signer_env):
    client, _, _, _ = evm_signer_env
    tx = evm_tx()
    del tx["nonce"]
    resp = client.post("/sign-evm", json={"transaction": tx}, headers=AUTH)
    assert resp.status_code == 400


def test_evm_to_allowlist(tmp_path, monkeypatch):
    from eth_account import Account

    account = Account.create()
    keyfile = tmp_path / "evm.key"
    keyfile.write_text(account.key.hex())
    monkeypatch.delenv("SIGNER_KEYPAIR_PATH", raising=False)
    monkeypatch.setenv("SIGNER_EVM_KEY_PATH", str(keyfile))
    monkeypatch.setenv("SIGNER_TOKEN", "signer-test-token")
    monkeypatch.setenv("SIGNER_EVM_TO_ALLOWLIST", "0x" + "22" * 20)
    module = load_signer_module()
    monkeypatch.setattr(module, "KILL_FILE", tmp_path / "SIGNER_KILLSWITCH")
    client = TestClient(module.create_app())
    resp = client.post("/sign-evm", json={"transaction": evm_tx()}, headers=AUTH)
    assert resp.status_code == 403
    assert "allowlist" in resp.json()["detail"]
    ok = client.post("/sign-evm", json={"transaction": {**evm_tx(),
                                                       "to": "0x" + "22" * 20}},
                     headers=AUTH)
    assert ok.status_code == 200
