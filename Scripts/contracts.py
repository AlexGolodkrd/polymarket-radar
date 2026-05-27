"""Typed contracts shared between the Python radar and the TypeScript
executor.

Why this file exists:
    Before, the radar built deal dicts in Python with ad-hoc keys, the
    HTTP dispatcher (`_fire_arb_via_ts`) JSON-encoded them, and the TS
    executor (`executor-ts/src/types/deal.ts`) parsed them back into
    its own definitions. The two sides diverged silently — `expectedPayout`
    was missing on the TS side for 3 hours of paper-trade-rejection
    (resolved in PR #168 only because operator noticed paper_stats=0%).

    This module is the **Python side of the contract**. The mirror lives
    in `executor-ts/src/types/deal.ts` — any field added here MUST be
    added there and vice versa. The two files reference each other in
    the file-header comments so reviewers can spot drift.

Migration plan:
    1. (this commit) Define Pydantic v2 models.
    2. (next commit) Replace ad-hoc dict construction in
       `Scripts/executor/atomic.py::_fire_arb_via_ts` with
       `FireRequest.model_dump_json()`.
    3. (next commit) Add a build-time codegen step that emits
       `executor-ts/src/types/contract.generated.ts` from these models
       via `pydantic-to-typescript` or `datamodel-code-generator`.
    4. Strip hand-written TS interfaces in deal.ts in favour of the
       generated file.

Until step 2 lands, these models are available for VALIDATION and
TYPE-CHECKING only — the wire format still goes through legacy dicts.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

try:
    from pydantic import BaseModel, ConfigDict, Field
    _PYDANTIC_AVAILABLE = True
except ImportError:
    _PYDANTIC_AVAILABLE = False


PlatformName = Literal[
    'Polymarket', 'Limitless', 'SX Bet', 'Kalshi',
    'Limitless+SX Bet', 'Polymarket+SX Bet', 'Polymarket+Limitless',
]

ArbStructure = Literal['all_yes', 'all_no', 'yes_no_pair', 'binary', 'cross_platform']

CrossStructure = Literal['X1', 'X2']

LegPlatform = Literal['polymarket', 'limitless', 'sx_bet', 'kalshi']

OrderSide = Literal['BUY', 'SELL']


if _PYDANTIC_AVAILABLE:

    class _StrictModel(BaseModel):
        """Base for all wire-format models. Forbids unknown fields so
        wire drift surfaces immediately rather than going silent."""
        model_config = ConfigDict(extra='forbid', frozen=False)

    class LegEntry(_StrictModel):
        """One leg of an arb. Mirrors the TS `LegEntry` type.

        Identifier fields are platform-specific and at least one MUST be
        populated per leg (the executor uses these to build EIP-712
        payloads and to look up the maker order). The Python radar
        is responsible for populating ALL fields the platform needs.
        """

        platform: LegPlatform = Field(description="Lowercase platform code")
        side: OrderSide = Field(description="BUY or SELL from our wallet's perspective")
        price: float = Field(ge=0.0, le=1.0, description="Raw 0..1, NOT cents")
        price_cents: float = Field(ge=0.0, le=100.0, description="Display value, snapshotted from `price`")
        size_usdc: float = Field(gt=0.0, description="Target size in USD-equivalent")

        # Platform-specific identifiers — at least one is required.
        token_id: Optional[str] = Field(default=None, description="Polymarket CTF token id (uint256 as str)")
        token_id_yes: Optional[str] = Field(default=None)
        token_id_no: Optional[str] = Field(default=None)
        condition_id: Optional[str] = Field(default=None, description="Polymarket conditionId (bytes32 hex)")
        market_hash: Optional[str] = Field(default=None, description="SX Bet bytes32 market hash")
        outcome_index: Optional[int] = Field(default=None, ge=1, le=255, description="SX outcome index (1 or 2)")
        slug: Optional[str] = Field(default=None, description="Limitless market slug")
        verifying_contract: Optional[str] = Field(default=None, description="Per-market for Limitless")

        # Metadata
        neg_risk: bool = Field(default=False, description="Polymarket negRisk routing")
        sport_type: Optional[str] = Field(default=None, description="For SX market-type checks (binary etc.)")
        effective_fee_bps: Optional[float] = Field(default=None, ge=0.0)
        expected_fill_price: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    class FireRequest(_StrictModel):
        """The wire payload from radar → TS executor.

        Fields are stable across the radar↔executor boundary. Adding a
        field here requires:
            1. matching field in executor-ts/src/types/deal.ts
            2. handler logic in executor-ts/src/executor/atomic.ts
            3. update to executor-ts/tests/server_metrics.test.ts
        """

        arb_id: str = Field(min_length=1, description="Stable id used for deduplication on the executor side")
        title: str = Field(min_length=1, max_length=500)
        platform: PlatformName
        arb_structure: ArbStructure
        cross_structure: Optional[CrossStructure] = Field(default=None)
        sum_cents: Optional[float] = Field(default=None, ge=0.0, le=300.0)
        net_expected_usd: float = Field(description="Net P&L expected at scan-time (after fees)")
        expected_payout_usd: float = Field(
            default=0.0, ge=0.0,
            description="PR #168 — gross payout the executor uses for size sanity",
        )
        dry_run: bool = Field(description="If True executor logs decisions, no real POSTs")
        first_seen_ts: Optional[float] = Field(default=None, description="Scan timestamp anchor for pipeline_timing")
        end_date: Optional[str] = Field(default=None, description="ISO-8601 UTC, when the event resolves")
        entries: list[LegEntry] = Field(min_length=1, max_length=8)

    class FireResponse(_StrictModel):
        """Executor → radar reply. Shape mirrors what the TS service
        returns from POST /fire."""

        arb_id: str
        outcome: Literal['success', 'aborted', 'error', 'killed', 'malformed', 'no_wallets']
        aborted_reason: Optional[str] = Field(default=None)
        error_message: Optional[str] = Field(default=None)
        leg_count_filled: int = Field(default=0, ge=0)
        leg_count_total: int = Field(default=0, ge=0)
        sim_pnl_usd: Optional[float] = Field(default=None)
        latency_ms: Optional[float] = Field(default=None, ge=0.0)

else:
    # Pydantic absent — provide thin record-style classes so callers can
    # still import the names. NO validation in this mode.
    class _StrictModel:  # type: ignore[no-redef]
        def __init__(self, **kw: Any) -> None:
            for k, v in kw.items():
                setattr(self, k, v)

    class LegEntry(_StrictModel):  # type: ignore[no-redef]
        pass

    class FireRequest(_StrictModel):  # type: ignore[no-redef]
        pass

    class FireResponse(_StrictModel):  # type: ignore[no-redef]
        pass
