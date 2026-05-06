# executor-ts

TypeScript executor layer for plan-kapkan radar — **Phase TS-1 skeleton**.

> ⚠ Not yet wired into the production radar. The Python detector still
> calls Python `Scripts/executor/`. This package is being built in
> parallel as the long-term replacement (see `docs/TS_REWRITE_PLAN.md`
> in the repo root).

## What's here (Phase TS-1)

```
executor-ts/
├── package.json           # Node 20.10+, viem 2, vitest, tsx, biome
├── tsconfig.json          # strict, noUncheckedIndexedAccess, exactOptionalPropertyTypes
├── biome.json             # lint+format, single quotes, 2-space, 100col
├── src/
│   ├── index.ts           # smoke entry — `npm run dev` to verify toolchain
│   ├── types/
│   │   ├── deal.ts        # FireRequest / LegSpec / BuiltOrder contracts
│   │   ├── wallet.ts      # Wallet + Polymarket V2 topology helpers
│   │   └── eip712.ts      # Domain + types constants for Poly / SX / Limitless
│   └── builders/
│       └── poly.ts        # buildPolyOrder — EIP-712 V2 sign via viem
└── tests/
    └── builders/
        └── poly.test.ts   # GOLDEN: byte-identical signatures with Python
```

## Why golden tests

Polymarket V2's CTF Exchange contract validates `ECDSA-recover(sig) == signer`
on every order. **One bit of drift between Python's eth_account path and
TS's viem path = every order rejected.** The `poly.test.ts` golden tests
sign a deterministic fixture (fixed privkey, salt, timestamp, token) and
compare bytes against the signature Python produces from the same inputs.
Until those goldens pass, this builder is NOT safe to wire into the
production fire path.

To re-generate the goldens after a Python-side change:
```bash
# from repo root
python -c "$(cat <<'EOF'
import sys; sys.path.insert(0, 'Scripts')
from eth_account import Account
from eth_account.messages import encode_typed_data
from executor.builders import (POLY_DOMAIN_STANDARD, POLY_DOMAIN_NEGRISK,
                                POLY_ORDER_TYPES_V2, ZERO_BYTES32)
PRIVKEY = '0x' + '11' * 32
order = {
    'salt': 12345678901234567890, 'maker': Account.from_key(PRIVKEY).address,
    'signer': Account.from_key(PRIVKEY).address,
    'tokenId': 71321045679252212594626385532706912750332728571942442218381354637562416002854,
    'makerAmount': 10_000_000, 'takerAmount': 20_000_000,
    'side': 0, 'signatureType': 0, 'timestamp': 1730000000000,
    'metadata': bytes(32), 'builder': bytes(32),
}
for d, label in [(POLY_DOMAIN_STANDARD, 'STANDARD'),
                 (POLY_DOMAIN_NEGRISK, 'NEGRISK')]:
    full = {'types': POLY_ORDER_TYPES_V2, 'primaryType': 'Order',
            'domain': d, 'message': order}
    sig = Account.sign_message(encode_typed_data(full_message=full),
                               private_key=PRIVKEY).signature.hex()
    print(label, '0x' + sig if not sig.startswith('0x') else sig)
EOF
)"
```

Paste the new hex strings into `PYTHON_GOLDEN_STANDARD` /
`PYTHON_GOLDEN_NEGRISK` in `tests/builders/poly.test.ts`.

## What's NOT here (later phases)

- **TS-1 follow-ups:** SX Bet builder (`builders/sx.ts`) and Limitless
  builder (`builders/limitless.ts`) with their own golden tests —
  same domain table extension as Poly.
- **TS-2:** Wallet pool + stores (LocalEnvStore, AwsSecretsStore).
- **TS-3:** `fireArb` engine, `POST /fire` endpoint, paper-trade pipeline.
- **TS-4:** Risk layer (limits, killswitch, network_check, reconcile).
- **TS-5:** Fill confirmation via WS, presign, maker mode.
- **TS-6:** Polymarket L2 HMAC + on-chain approvals.
- **TS-7:** Cutover from Python in-process executor.

Full plan: [`docs/TS_REWRITE_PLAN.md`](../docs/TS_REWRITE_PLAN.md).

## Local development

```bash
# from this directory
npm install            # pulls viem + vitest + tsx + biome
npm run typecheck      # tsc --noEmit
npm test               # vitest run — must be 7/7 green for poly builder
npm run dev            # tsx watch src/index.ts — smoke entry
npm run lint           # biome check
```

Node 20.10+ required (uses `crypto.randomUUID` + `crypto.getRandomValues`
in the salt generator).

## Why TypeScript

See `docs/TS_REWRITE_PLAN.md §0`. TL;DR: detection logic stays Python
(stable, tested), but the executor wins on:

- **EIP-712 signing** — viem's typed data API is 1:1 with Solidity types,
  prevents the regression class Python had in v17/v19/v23/v24.
- **BigInt math** — native, no float drift on `usdc * 1e6` round-trip.
- **HTTP latency** — undici keep-alive + HTTP/2, ~3× faster than `requests`.
- **Async firing** — Promise.all replaces Python ThreadPoolExecutor + GIL.
- **Type-safe contracts** between detector and executor via JSON schema.
