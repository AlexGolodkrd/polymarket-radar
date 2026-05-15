# SX Bet Trading

Updated 2026-05-15 against SX Bet v2 protocol (live-verified). The earlier v1-style version of this skill (orderHashes / greedy match / `/v1/orders/fill/v2`) is dead — SX rewrote the fill protocol. **This file is the authority.**

## What this is for

SX Bet (sx.bet) is a decentralized sports betting exchange on SX Network (chainId 4162). Sport-only. From the radar's perspective it's the **taker side** of cross-platform arbs (binary markets, mirrors Polymarket structure).

## Endpoint

```
POST https://api.sx.bet/orders/fill/v2          ← canonical, NO /v1 prefix
GET  https://api.sx.bet/orders?marketHashes=X   ← orderbook (public, no auth)
GET  https://api.sx.bet/metadata                ← contract addresses, base tokens
```

## Auth

No API key. Wallet's EIP-712 signature authenticates.

## EIP-712 typed data (v2 — current)

```js
domain = {
  name: "SX Bet",                                                       // NOT "SX Bet Order Fill"
  version: "6.0",                                                       // from /metadata.domainVersion
  chainId: 4162,
  verifyingContract: "0x845a2Da2D70fEDe8474b1C8518200798c60aC364"        // EIP712FillHasher from /metadata
};

types = {
  Details: [
    { name: "action",         type: "string" },
    { name: "market",         type: "string" },
    { name: "betting",        type: "string" },
    { name: "stake",          type: "string" },
    { name: "worstOdds",      type: "string" },
    { name: "worstReturning", type: "string" },   // new in v2
    { name: "fills",          type: "FillObject" } // nested
  ],
  FillObject: [
    { name: "stakeWei",                 type: "string" },
    { name: "marketHash",               type: "string" },
    { name: "baseToken",                type: "string" },
    { name: "desiredOdds",              type: "string" },
    { name: "oddsSlippage",             type: "uint256" },
    { name: "isTakerBettingOutcomeOne", type: "bool" },
    { name: "fillSalt",                 type: "uint256" },
    { name: "beneficiary",              type: "address" },
    { name: "beneficiaryType",          type: "uint8" },
    { name: "cashOutTarget",            type: "bytes32" }
  ]
};

primaryType: "Details"

// Details fields take literal "N/A" placeholders — only `fills` has real data
message = {
  action: "N/A", market: marketHash, betting: "N/A",
  stake: "N/A", worstOdds: "N/A", worstReturning: "N/A",
  fills: {
    stakeWei, marketHash, baseToken,
    desiredOdds,           // taker_price * 1e20 as string
    oddsSlippage: 0n,      // bigint
    isTakerBettingOutcomeOne,
    fillSalt: BigInt(fillSalt),
    beneficiary: ZeroAddress, beneficiaryType: 0,
    cashOutTarget: ZeroBytes32
  }
};
```

## POST body shape

```json
{
  "market": "<real marketHash, NOT 'N/A' despite docs example>",
  "baseToken": "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B",
  "isTakerBettingOutcomeOne": true|false,
  "stakeWei": "<integer in 1e6 USDC>",
  "desiredOdds": "<taker_price × 1e20>",
  "oddsSlippage": 0,
  "taker": "<wallet address>",
  "takerSig": "<EIP-712 sig>",
  "fillSalt": "<uint256 decimal>",
  "message": "N/A"
}
```

## `desiredOdds` direction (the easy bug)

`desiredOdds = implied_probability * 1e20 = taker_price * 1e20`. NOT `(1 - taker_price)`.

For takerPrice 0.4 + slippage 0.005 → desiredOdds = "40500000000000000000". Inverted direction = `NO_MATCHING_ORDERS` even when liquidity exists.

## Binary market semantics

SX markets are binary YES/NO on a single statement ("Team A wins?"). The SX UI labels outcome 2 with the **opposite team's name** but mechanically:
- outcome 1 = "Team A wins" YES
- outcome 2 = "Team A wins" NO = (Team B wins) OR (Draw)

This is critical for cross-platform arb math. See `project_cp_arb_strategy.md` in memory.

## Response

```json
{
  "status": "success",
  "data": {
    "fillHash": "0x...",
    "isPartialFill": false,
    "totalFilled": "1000000",      // wei; ZERO = soft no-op, NOT a fill
    "averageOdds": "28375000000000000000"
  }
}
```

Treat as filled only if `totalFilled > 0`. `fillHash` alone is not proof.

## Common errors

| Body | Cause |
|---|---|
| `"TAKER_INSUFFICIENT_BASE_TOKEN_ALLOWANCE"` | USDC not approved to `TokenTransferProxy = 0x38aef22152BC8965bf0af7Cf53586e4b0C4E9936`. Operator fix: place $1 manual bet via sx.bet UI. |
| `"INSUFFICIENT_TAKER_BALANCE"` | Wallet USDC on SX Rollup < stake. |
| `"NO_MATCHING_ORDERS"` | Either `desiredOdds` inverted, or real liquidity gap. |
| `"market must be a valid hex string of length 32 bytes"` | body.market is not real marketHash. |
| `"Invalid signature ..."` | Domain/types/message mismatch. |

## Proxy policy

Per operator rule: residential SOCKS5 (`pool.proxy.market`) is used **only for the POST `/orders/fill/v2`** call. Public reads (`/metadata`, `/orders` orderbook, `/markets`) go direct from VPS. Proxy keepalive default disabled (port-based sticky).
