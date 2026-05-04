"""Paper trading validation + graduation gate (Phase 5 — PR #16).

Phase 2 already writes paper_results.jsonl with realistic-fill drift after
each dry-fired arb. Phase 5 adds:
    - GraduationGate: tracks rolling-100 win rate + drift, returns
      whether the operator can flip DRY_RUN=0 (>=70% win, <=20% drift).
    - graduation_history(): per-day rolling stats for the dashboard chart.
    - paper_distribution(): bins of P&L outcomes for a histogram.
    - first_real_trades_size(): suggests $5/leg for the first 10 real
      trades after graduation, then ramps to full $55.

The graduation thresholds match the original plan + feedback:
    - 100 paper trades minimum
    - >= 70% positive realistic_pnl_5s ("win rate")
    - <= 20% mean drift |realistic - sim|
After graduation, first 10 real trades use $5/leg (not $55) — that's a
final sanity calibration before ramping up.
"""
import json
import logging
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_THIS_DIR, '..'))
EXECUTIONS_DIR = os.path.join(_REPO_ROOT, 'Executions')
PAPER_RESULTS_PATH = os.path.join(EXECUTIONS_DIR, 'paper_results.jsonl')
PAPER_GRADUATION_LOG = os.path.join(EXECUTIONS_DIR, 'paper_graduation.jsonl')

# Graduation thresholds (from feedback + plan).
# Phase 9jjj (30.04.2026) — operator request: drop 100 -> 50.
# Reason: Friday->Saturday night gave 1 arb in 10 hours. To accumulate 100
# paper trades at this rate would take days/weeks. 50 is enough statistical
# significance for first iteration of live trading (10 real trades follow
# at $5/leg per FIRST_REAL_TRADES_COUNT).
# Override at runtime via env GRADUATION_MIN_TRADES=N.
GRADUATION_MIN_TRADES = int(os.environ.get('GRADUATION_MIN_TRADES', '50'))
GRADUATION_MIN_WIN_RATE = 0.70    # 70%
GRADUATION_MAX_DRIFT = 0.20       # 20%
FIRST_REAL_TRADES_COUNT = 10      # first N real trades after graduation
FIRST_REAL_LEG_SIZE_USDC = 5.0    # leg size for those first trades


@dataclass
class GraduationStatus:
    count: int                    # paper trades observed in window
    win_rate: Optional[float]
    mean_drift: Optional[float]
    mean_slippage_cents: Optional[float]
    median_pnl: Optional[float]
    ready: bool
    blockers: list                # human-readable list of unmet conditions
    next_threshold_hint: str      # what to watch for to flip ready

    def to_dict(self):
        return {
            'count': self.count,
            'win_rate_pct': round(100 * self.win_rate, 1) if self.win_rate is not None else None,
            'mean_drift': round(self.mean_drift, 4) if self.mean_drift is not None else None,
            'mean_slippage_cents': self.mean_slippage_cents,
            'median_pnl': self.median_pnl,
            'graduation_ready': self.ready,
            'min_trades_required': GRADUATION_MIN_TRADES,
            'min_win_rate_pct': GRADUATION_MIN_WIN_RATE * 100,
            'max_drift_pct': GRADUATION_MAX_DRIFT * 100,
            'blockers': self.blockers,
            'next_threshold_hint': self.next_threshold_hint,
            'first_real_size_usdc': FIRST_REAL_LEG_SIZE_USDC,
            'first_real_count': FIRST_REAL_TRADES_COUNT,
        }


def _read_paper_results(window_n: int = 100) -> list:
    if not os.path.exists(PAPER_RESULTS_PATH):
        return []
    rows = []
    with open(PAPER_RESULTS_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            try: rows.append(json.loads(line))
            except: continue
    return rows[-window_n:]


def graduation_status(window_n: int = GRADUATION_MIN_TRADES) -> GraduationStatus:
    """Roll up the last `window_n` paper-trade results into a GraduationStatus.

    The blockers list is human-friendly so the dashboard can show
    ('need 17 more trades · win rate 65% (need 70%)') verbatim.
    """
    rows = _read_paper_results(window_n)
    n = len(rows)
    if n == 0:
        return GraduationStatus(
            count=0, win_rate=None, mean_drift=None,
            mean_slippage_cents=None, median_pnl=None,
            ready=False,
            blockers=[f'no paper trades yet — need {GRADUATION_MIN_TRADES}'],
            next_threshold_hint='Wait for the radar to scan — auto-fire'
                                ' will populate paper_results.jsonl',
        )

    wins = sum(1 for r in rows if (r.get('realistic_pnl_5s') or 0) > 0)
    win_rate = wins / n
    drifts = [r['drift'] for r in rows if r.get('drift') is not None]
    mean_drift = sum(drifts) / len(drifts) if drifts else None
    pnls = sorted([r['realistic_pnl_5s'] for r in rows if r.get('realistic_pnl_5s') is not None])
    # Phase 19v13 (05.05.2026) — true median: average of two middle values
    # for even-length lists (was off-by-one — picked the upper middle and
    # called it median, slightly biased upward for skewed distributions).
    if pnls:
        m = len(pnls)
        if m % 2 == 1:
            median_pnl = pnls[m // 2]
        else:
            median_pnl = (pnls[m // 2 - 1] + pnls[m // 2]) / 2
    else:
        median_pnl = None

    slips = [s.get('slippage') for r in rows for s in r.get('legs', [])
             if isinstance(s, dict) and s.get('slippage') is not None]
    mean_slip_c = round(100 * sum(slips) / len(slips), 3) if slips else None

    blockers = []
    if n < GRADUATION_MIN_TRADES:
        blockers.append(f'need {GRADUATION_MIN_TRADES - n} more trades '
                        f'({n}/{GRADUATION_MIN_TRADES})')
    if win_rate < GRADUATION_MIN_WIN_RATE:
        blockers.append(f'win rate {100*win_rate:.1f}% '
                        f'(need {100*GRADUATION_MIN_WIN_RATE:.0f}%)')
    if mean_drift is not None and mean_drift > GRADUATION_MAX_DRIFT:
        blockers.append(f'mean drift {100*mean_drift:.1f}% '
                        f'(need ≤{100*GRADUATION_MAX_DRIFT:.0f}%)')

    ready = not blockers
    if ready:
        hint = (f'🎓 ready — flip DRY_RUN=0 for first {FIRST_REAL_TRADES_COUNT} '
                f'real trades at ${FIRST_REAL_LEG_SIZE_USDC:.0f}/leg')
    elif n < GRADUATION_MIN_TRADES:
        hint = f'collecting paper trades ({n}/{GRADUATION_MIN_TRADES})'
    else:
        hint = 'tune thresholds or quality guards — see blockers'

    return GraduationStatus(
        count=n, win_rate=win_rate, mean_drift=mean_drift,
        mean_slippage_cents=mean_slip_c, median_pnl=median_pnl,
        ready=ready, blockers=blockers, next_threshold_hint=hint,
    )


def paper_distribution(window_n: int = 500) -> dict:
    """P&L histogram bins for the dashboard chart. Bins are in dollars."""
    rows = _read_paper_results(window_n)
    if not rows:
        return {'bins': [], 'counts': [], 'total': 0}
    edges = [-2.0, -1.0, -0.5, -0.1, 0, 0.1, 0.5, 1.0, 2.0, 5.0]
    labels = ['<−$2', '−$2..−$1', '−$1..−$0.50', '−$0.50..−$0.10',
              '−$0.10..0', '0..$0.10', '$0.10..$0.50', '$0.50..$1',
              '$1..$2', '$2..$5', '>$5']
    counts = [0] * len(labels)
    for r in rows:
        p = r.get('realistic_pnl_5s')
        if p is None: continue
        idx = 0
        for i, e in enumerate(edges):
            if p < e:
                idx = i; break
        else:
            idx = len(labels) - 1
        counts[idx] += 1
    return {'bins': labels, 'counts': counts, 'total': sum(counts)}


def graduation_history(days: int = 14) -> list:
    """Daily rolling win-rate over the last `days` UTC days. Used for the
    dashboard time-series chart so the operator can see the trajectory.
    """
    if not os.path.exists(PAPER_RESULTS_PATH):
        return []
    by_date: dict = defaultdict(list)
    cutoff = time.time() - days * 86400
    with open(PAPER_RESULTS_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            try: row = json.loads(line)
            except: continue
            ev = row.get('evaluated_at') or row.get('dry_fired_at')
            if not ev or ev < cutoff: continue
            d = datetime.fromtimestamp(ev, tz=timezone.utc).strftime('%Y-%m-%d')
            by_date[d].append(row)
    out = []
    for d in sorted(by_date.keys()):
        rows = by_date[d]
        n = len(rows)
        if n == 0: continue
        wins = sum(1 for r in rows if (r.get('realistic_pnl_5s') or 0) > 0)
        drifts = [r['drift'] for r in rows if r.get('drift') is not None]
        out.append({
            'date': d, 'count': n,
            'win_rate_pct': round(100 * wins / n, 1),
            'mean_drift_pct': round(100 * sum(drifts) / len(drifts), 1) if drifts else None,
        })
    return out


# ── Phase 6 readiness — first-real-trade size suggestion ───────────
def first_real_trade_size_usdc(real_trade_count: int) -> float:
    """After graduation, first FIRST_REAL_TRADES_COUNT real trades use
    a small leg size (FIRST_REAL_LEG_SIZE_USDC = $5) for final calibration.
    After that the executor uses the stake size from the deal builder
    (capped by Phase 3's MAX_PER_TRADE_USD = $55)."""
    if real_trade_count < FIRST_REAL_TRADES_COUNT:
        return FIRST_REAL_LEG_SIZE_USDC
    return None  # signal "use full deal-builder stake, subject to risk gates"
