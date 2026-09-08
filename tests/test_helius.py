"""Helius provider: swap parsing, holder summary, API-key hygiene.

Fixtures mirror the documented Helius Enhanced Transactions / DAS response
shapes. No network calls.
"""
import pytest

from tradeos.logging_setup import redact
from tradeos.providers.chains import build_chain_providers
from tradeos.providers.chains.helius import (
    WSOL_MINT, HeliusProvider, parse_enhanced_swap)

MINT = "MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WALLET = "Wa11etAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def buy_tx(sig="sig_buy", sol=2.0, tokens=1000.0, ts=1_757_000_000):
    return {
        "signature": sig, "type": "SWAP", "source": "JUPITER",
        "feePayer": WALLET, "timestamp": ts,
        "events": {"swap": {
            "nativeInput": {"account": WALLET, "amount": str(int(sol * 1e9))},
            "nativeOutput": None,
            "tokenInputs": [],
            "tokenOutputs": [{
                "userAccount": WALLET, "mint": MINT,
                "rawTokenAmount": {"tokenAmount": str(int(tokens * 1e6)),
                                   "decimals": 6}}],
        }},
    }


def sell_tx(sig="sig_sell", sol=3.0, tokens=1000.0, ts=1_757_000_500):
    return {
        "signature": sig, "type": "SWAP", "source": "RAYDIUM",
        "feePayer": WALLET, "timestamp": ts,
        "events": {"swap": {
            "nativeInput": None,
            "nativeOutput": {"account": WALLET, "amount": str(int(sol * 1e9))},
            "tokenInputs": [{
                "userAccount": WALLET, "mint": MINT,
                "rawTokenAmount": {"tokenAmount": str(int(tokens * 1e6)),
                                   "decimals": 6}}],
            "tokenOutputs": [],
        }},
    }


def test_parse_buy():
    rec = parse_enhanced_swap(buy_tx())
    assert rec is not None
    assert rec.direction == "buy"
    assert rec.wallet == WALLET
    assert rec.token_mint == MINT
    assert rec.sol_amount == 2.0
    assert rec.token_amount == 1000.0
    assert rec.block_time == 1_757_000_000


def test_parse_sell():
    rec = parse_enhanced_swap(sell_tx())
    assert rec is not None
    assert rec.direction == "sell"
    assert rec.sol_amount == 3.0


def test_parse_wsol_input_counts_as_sol():
    tx = buy_tx()
    tx["events"]["swap"]["nativeInput"] = None
    tx["events"]["swap"]["tokenInputs"] = [{
        "userAccount": WALLET, "mint": WSOL_MINT,
        "rawTokenAmount": {"tokenAmount": str(int(1.5e9)), "decimals": 9}}]
    rec = parse_enhanced_swap(tx)
    assert rec is not None
    assert rec.direction == "buy"
    assert rec.sol_amount == 1.5


def test_token_to_token_swap_skipped():
    tx = buy_tx()
    tx["events"]["swap"]["nativeInput"] = None
    tx["events"]["swap"]["tokenInputs"] = [{
        "userAccount": WALLET, "mint": "OtherMint111",
        "rawTokenAmount": {"tokenAmount": "5000000", "decimals": 6}}]
    assert parse_enhanced_swap(tx) is None


def test_non_swap_and_malformed_skipped():
    assert parse_enhanced_swap({"type": "TRANSFER", "signature": "s",
                                "feePayer": WALLET}) is None
    assert parse_enhanced_swap({"type": "SWAP"}) is None  # no signature/payer
    assert parse_enhanced_swap({"type": "SWAP", "signature": "s",
                                "feePayer": WALLET}) is None  # no swap event


async def test_holder_summary_from_das(monkeypatch):
    provider = HeliusProvider("test-key-not-real")
    accounts = [{"owner": f"owner{i}", "amount": str((20 - i) * 100)}
                for i in range(20)]

    async def fake_rpc(method, params):
        assert method == "getTokenAccounts"
        return {"token_accounts": accounts} if params["page"] == 1 else {"token_accounts": []}

    monkeypatch.setattr(provider, "_rpc", fake_rpc)
    summary = await provider.get_holder_summary(MINT)
    assert summary["source"] == "helius_das"
    assert summary["holder_count"] == 20
    total = sum((20 - i) * 100 for i in range(20))
    top10 = sum((20 - i) * 100 for i in range(10))
    assert summary["top10_holder_pct"] == round(top10 / total * 100, 2)
    assert summary["top20_holder_pct"] == 100.0
    await provider.close()


async def test_holder_summary_falls_back_when_das_empty(monkeypatch):
    provider = HeliusProvider("test-key-not-real")

    async def fake_rpc(method, params):
        if method == "getTokenAccounts":
            return None  # DAS unavailable
        if method == "getTokenLargestAccounts":
            return {"value": [{"uiAmount": 600.0}, {"uiAmount": 400.0}]}
        if method == "getTokenSupply":
            return {"value": {"uiAmount": 2000.0}}
        return None

    monkeypatch.setattr(provider, "_rpc", fake_rpc)
    summary = await provider.get_holder_summary(MINT)
    assert summary["source"] == "solana_rpc"
    assert summary["top10_holder_pct"] == 50.0
    await provider.close()


async def test_address_swaps_parses_and_filters(monkeypatch):
    provider = HeliusProvider("test-key-not-real")

    async def fake_get(path, params, retries=3):
        assert path == f"/v0/addresses/{WALLET}/transactions"
        assert params["type"] == "SWAP"
        return [buy_tx(), sell_tx(), {"type": "TRANSFER", "signature": "x",
                                      "feePayer": WALLET}]

    monkeypatch.setattr(provider, "_get_json", fake_get)
    records = await provider.get_address_swaps(WALLET)
    assert [r.direction for r in records] == ["buy", "sell"]
    await provider.close()


def test_helius_selected_when_key_present(monkeypatch):
    monkeypatch.setenv("TRADEOS_HELIUS_API_KEY", "fake-key-for-test")
    providers = build_chain_providers()
    assert isinstance(providers["solana"], HeliusProvider)
    monkeypatch.delenv("TRADEOS_HELIUS_API_KEY")
    providers2 = build_chain_providers()
    assert not isinstance(providers2["solana"], HeliusProvider)


def test_api_key_urls_are_redacted():
    leaked = "request to https://mainnet.helius-rpc.com/?api-key=abcd1234efgh5678 failed"
    assert "abcd1234" not in redact(leaked)
    assert "[REDACTED]" in redact(leaked)
