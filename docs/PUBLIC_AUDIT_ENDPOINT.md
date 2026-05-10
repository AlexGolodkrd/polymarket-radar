# Public read-only audit endpoint — `/api/recent_deals`

## What it is

A **single** unauthenticated GET endpoint that returns the last N
analytics events with all PII fields stripped. Lets external observers
(and the agent maintaining this radar) verify deal flow without nginx
basic-auth credentials.

**URL:** `https://kapkan.4frdm.live/api/recent_deals?limit=50`

**Returns:** `{rows: [...], count: N}`

Each row is whitelisted to economic + structural fields only:
- Time / identity: `ts`, `arb_id`, `key`, `type`
- Market structure: `title`, `platform`, `arb_structure`, `cross_structure`
- Economics: `sum_cents`, `net`, `gross`, `fee`, `roi`, `adj_roi`, `grade`
- Quality: `min_liq`, `balance_used`, `theta`, `confidence`, `end_date`

**What's NOT exposed:**
- Token IDs (Polymarket CTF, Limitless tokens)
- Market hashes (SX Bet)
- Wallet addresses / signers / makers
- EIP-712 signatures
- Salt values
- L2 API keys / secrets / passphrases
- Verifying contracts
- Per-leg POST body / order struct
- Per-leg `entries[]` array (each leg has token + price + stake)

The whitelist lives in `Scripts/arb_server.py` as `ALLOWED_DEAL_FIELDS`.
Adding a field there exposes it to the public — review carefully.

## Why this is safe to expose

Every field in the allowlist is **already visible** on the dashboard's
public landing page (`https://kapkan.4frdm.live/`) for the active
deals widget. We're not opening new information — we're just providing
a programmatic way to read the same numbers that any visitor with the
URL can already see.

Nothing in the allowlist enables:
- Forging an order (no token IDs, no signatures)
- Tracing a wallet (no addresses, no proxy/funder)
- Re-deriving credentials (no API keys, no salts)
- Flooding write endpoints (no auth/identity required and granted)

## Required nginx change

The endpoint must be whitelisted from basic auth. Add this **inside**
the existing `server { ... }` block on the VPS, BEFORE any catch-all
`location /` or `location /api/` block that has `auth_basic`:

```nginx
# /etc/nginx/sites-available/kapkan.4frdm.live
location = /api/recent_deals {
    auth_basic off;
    proxy_pass http://localhost:5050/api/recent_deals;
    proxy_set_header Host $host;
    proxy_read_timeout 10s;
}
```

Note `=` for exact-match — only `/api/recent_deals` (with optional
`?...` query string) is whitelisted. `/api/recent_dealsX` or
`/api/recent_deals/foo` still hit the auth_basic block.

Reload nginx after editing:
```bash
sudo nginx -t && sudo systemctl reload nginx
```

## Verify

After the workflow auto-deploys + nginx reload:

```bash
# Public probe (no auth needed)
curl -sf https://kapkan.4frdm.live/api/recent_deals?limit=5 | python3 -m json.tool

# Auth-protected siblings (still 401)
curl -i https://kapkan.4frdm.live/api/analytics      # → 401
curl -i https://kapkan.4frdm.live/api/scan_state     # → 401
```

## Privacy / security checklist

| Concern | Mitigation |
|---|---|
| Order forgery | Signatures + salts excluded |
| Wallet enumeration | All addresses excluded |
| Credential leak | API keys / passphrases never even loaded into the analytics file |
| Trade size doxxing | `balance_used` + `min_liq` revealed but already visible on the dashboard |
| Strategy IP | Allowlist + structural fields only — competitors can't recreate the detection algo from this |
| DoS via large limit | Server-side cap at 500 |
| File disk usage | Endpoint reads last ~2KB × limit chunk only, no full-file scan |

## Disabling

If concerns arise: revert the nginx whitelist (`auth_basic off` block)
— the endpoint goes back behind 401 immediately, no Python-side change
needed. Or drop the route in `arb_server.py`.
