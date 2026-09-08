# TradeOS External Signer

The one place the private key exists. TradeOS never sees it — it sends
unsigned transactions here and gets back a signed transaction or a refusal.

## Security model

- Run as a **different OS user** than TradeOS (ideally a different host or
  container). TradeOS reaches it only via `TRADEOS_SIGNER_URL`.
- Its own independent policy, enforced regardless of what TradeOS asks:
  bearer-token auth, fee-payer-must-be-own-key, rolling hourly rate limit,
  and a filesystem kill switch (`touch SIGNER_KILLSWITCH`).
- The keypair file must be readable only by the signer user (`chmod 400`).
- Fund this wallet **only with what it may lose**. Treasury stays elsewhere.
- This is a reference implementation. For serious balances use a hardware
  wallet or HSM-backed signer and add a program-id allowlist.

## Run

```bash
pip install fastapi uvicorn solders
export SIGNER_KEYPAIR_PATH=/home/signer/.config/solana/id.json  # chmod 400
export SIGNER_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
export SIGNER_MAX_PER_HOUR=30
uvicorn 'solana_signer:app' --factory --host 127.0.0.1 --port 8471
```

Then in TradeOS's `.env`:

```
TRADEOS_SIGNER_URL=http://127.0.0.1:8471
TRADEOS_SIGNER_TOKEN=<the same token>
```

## Emergency stop

`touch signer/SIGNER_KILLSWITCH` — refuses all signing immediately, even
if TradeOS or its API is compromised or unreachable. Independent of the
TradeOS kill switch.
