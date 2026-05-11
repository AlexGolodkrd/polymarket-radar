---
name: eip712-typescript-parity
description: Reproducing exact EIP-712 signature bytes from Python (eth-account) in TypeScript (viem). Covers Polymarket V2 CLOB order signing, SX Bet order signing, and Limitless CTF order signing — including the byte-for-byte gotchas that produced silent 401/422 errors in the executor-ts migration. Use whenever you port a Python-side signature to TypeScript or are debugging an exchange that rejects a freshly-signed order.
---

# EIP-712 TypeScript Parity — Python → viem migration

When porting `py-clob-client` / `sx-bet-sdk` / `limitless-sdk` order signing to TypeScript, you can produce a signature that LOOKS valid (66 hex chars, recovers to the right address) but the exchange rejects it. The cause is almost always a byte-level encoding difference in one of the EIP-712 fields. This skill catalogs every divergence we hit in TS-5 (Polymarket V2 + SX Bet + Limitless).

## The hashing pipeline (refresher)

EIP-712 v4 hashes `0x1901 || domainSeparator || structHash(message)`. Both sides must agree on:
1. The **domain separator** — name, version, chainId, verifyingContract, salt
2. The **type definitions** — exact field names, exact types, exact order
3. The **encoded message** — each field encoded per its type, then `keccak256`'d together
4. The **signing key** — see `secrets-management` skill for key normalization

If either side has a typo in the type name, gets `chainId` wrong, includes a stale field, or pads a number differently, the resulting hashes diverge and the exchange's signature recovery yields a different address than `from`.

## Polymarket V2 CLOB

**Domain:**
```ts
{
  name: 'Polymarket CTF Exchange',
  version: '1',
  chainId: 137,
  verifyingContract: '0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E' as Hex,
}
```

**Order type (verify order MATCHES the Python side exactly):**
```ts
{
  Order: [
    { name: 'salt', type: 'uint256' },
    { name: 'maker', type: 'address' },
    { name: 'signer', type: 'address' },
    { name: 'taker', type: 'address' },
    { name: 'tokenId', type: 'uint256' },
    { name: 'makerAmount', type: 'uint256' },
    { name: 'takerAmount', type: 'uint256' },
    { name: 'expiration', type: 'uint256' },
    { name: 'nonce', type: 'uint256' },
    { name: 'feeRateBps', type: 'uint256' },
    { name: 'side', type: 'uint8' },
    { name: 'signatureType', type: 'uint8' },
  ],
}
```

**Field gotchas we hit:**
- `signatureType: 0` for EOA (Metamask). `1` for Magic, `2` for Gnosis Safe. Python defaulted to `0` silently; TS code had to set it explicitly via `as const` or the type erasure produced `bigint` and rejected.
- `salt` and `expiration` MUST be strings of decimal digits when passed to viem's `signTypedData`. Pass `BigInt(value)` not `value.toString()` — viem will throw on string. Python's `web3.py` accepted both.
- `tokenId` is the CLOB token ID (NOT the conditionId). 64-char hex string with **0x prefix removed** and treated as `uint256`. Python's `int(token_id, 16)`; TS does `BigInt('0x' + token_id_hex)`.

**Signing (viem):**
```ts
import { privateKeyToAccount } from 'viem/accounts';
const account = privateKeyToAccount(privateKey);
const signature = await account.signTypedData({
  domain,
  types: { Order: [...] },
  primaryType: 'Order',
  message,
});
// signature is 0x{r}{s}{v} as a single hex string — same format py-clob-client emits.
```

**Verify in 1 line:**
```ts
import { verifyTypedData } from 'viem';
const ok = await verifyTypedData({ address: account.address, domain, types, primaryType: 'Order', message, signature });
console.log({ ok, expectedSigner: account.address });
```
If `ok === false`, your hash didn't match what the contract will compute. Compare Python's `eip712_structured_data.hashStruct(message)` and viem's `hashTypedData({domain,types,...})` side-by-side.

## SX Bet OrderFill

**Domain (Polygon mainnet):**
```ts
{
  name: 'SX Bet',
  version: '6.0',
  chainId: 137,
  verifyingContract: '0x5f15a4F1d0a18d22b5b50B6f5DA4B6F0aE05B41B' as Hex,
}
```

**Type (note the unusual field order — exchange-specific):**
```ts
{
  Details: [
    { name: 'action', type: 'string' },
    { name: 'market', type: 'string' },
    { name: 'betSize', type: 'string' },
    { name: 'odds', type: 'string' },
    { name: 'nonce', type: 'string' },
    { name: 'executor', type: 'string' },
  ],
}
```

**Gotcha:** SX uses `string` for everything, including `odds` and `betSize`. Python sends them as decimal strings (`'0.5'`). TypeScript must do the same — viem will accept numbers but the resulting hash differs from what SX's signature-recovery code computes. Always wrap in `String(...)`.

## Limitless CTF

**Domain:**
```ts
{
  name: 'Limitless CTF Exchange',
  version: '1',
  chainId: 8453,  // Base, not Polygon
  verifyingContract: '0x...',  // grep the production code
}
```

**Type:** Same shape as Polymarket V2's `Order` (it's a fork). But `chainId` is Base (8453), not Polygon. Mixing up chainId is the single most common cause of "valid-looking signature, server rejects." Always log `chainId` on both sides during debugging.

## Decision tree — "my signature is rejected"

1. Does the **address recover correctly** (`verifyTypedData` returns true)?  
   → NO: domain/type/message divergence. See § Verify in 1 line.  
   → YES: keep going.
2. Are you signing with the **right account** (signer vs funder for V2)?  
   → If funder addr differs from signer addr, set `signatureType: 1` or `2` and the funder address as `maker`, signer as `signer`. See `polymarket-v2-auth` skill.
3. Are the **L2 headers (POLY-API-KEY etc.)** correct in addition to the order signature?  
   → Order signature ≠ L2 auth. POST to `/order` needs BOTH. See `polymarket-v2-troubleshoot`.
4. Has the **nonce** been incremented since you last signed?  
   → Polymarket allows nonce reuse only for orders that haven't been broadcast. SX rejects reuse always. Limitless ignores nonce (server-side).
5. Is the **chainId** Polygon (137) vs Base (8453) vs Mainnet (1)?  
   → Wrong chainId → silent rejection (no error message in some exchanges).

## See also

- `polymarket-v2-auth` — L1 vs L2 auth, signature_type 0/1/2 decision tree
- `polymarket-v2-troubleshoot` — failure → cause matrix
- `polymarket-v2-connector` — drop-in TS reference (this skill's sibling)
- `sx-bet-trading` — order shape
- `limitless-trading` — CTF order shape
- `secrets-management` — private key normalization (whitespace, case, 0x prefix)
