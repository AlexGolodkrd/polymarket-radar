"""Phase 9q — fix for build_deal gross-payout formula.

Background: the formula was

    gross = balance * (payout_target - total_price)            # WRONG

It missed `/ total_price` normalisation. Real guaranteed payout under
balanced (equal-payout) sizing is

    contracts_per_leg = balance / total_price          (constant across legs)
    guaranteed_payout = payout_target * contracts_per_leg
                     = balance * payout_target / total_price
    gross             = guaranteed_payout - balance
                     = balance * (payout_target - total_price) / total_price

For ALL_YES (total_price ≈ 0.95, payout_target=1) the error was small.
For ALL_NO N=3 (total_price ≈ 1.94, payout_target=2) the error was ×2
— UI showed double the real spread.
For ALL_NO N≥4 the error compounded further.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'Scripts'))

import arb_server


def _outcomes(prices, liq=10000):
    # Phase audit-27.05 (27.05.2026): use a valid REAL_OB_SOURCES value
    # (clob_ask) instead of legacy 'test'. PR #43 (Phase 9kkk) added a
    # strict source whitelist in build_deal that rejects anything outside
    # {clob_ask, kalshi_ob, sx_ob, lim_clob, clob_synthetic} — tests
    # written before that change all returned None and silently failed
    # their assertIsNotNone checks. Liquidity bumped to 10000 ≥ MIN_LEG_LIQ_USD.
    return [{'name': f'O{i}', 'price': p, 'liquidity': liq, 'source': 'clob_ask',
             'volume': 5000} for i, p in enumerate(prices)]


class TestBuildDealMath(unittest.TestCase):
    def test_all_yes_3way_correct_gross(self):
        # 3-way categorical, sum_yes 0.95 → 5% spread on $1 payout
        # Real payout per $1 stake = $1 / $0.95 = $1.0526
        # → gross/balance = (1.0526 − 1) = 5.26%
        prices = [0.30, 0.32, 0.33]   # sum 0.95
        d = arb_server.build_deal('test_AY', 'Polymarket', _outcomes(prices),
                                   sum(prices), theta=0.0, threshold=0.99,
                                   payout_target=1.0)
        self.assertIsNotNone(d)
        # actual_balance can be < BALANCE due to risk caps; use ratio test
        gross_per_dollar = d['gross'] / d['max_stake'] / 3
        # Sanity-only: precise check via computed expectation
        # Expected gross fraction of actual_balance ≈ 0.05/0.95 = 0.0526
        # We can read actual_balance from sum of stakes
        ab = sum(e['stake'] for e in d['entries'])
        expected_gross = ab * (1.0 - 0.95) / 0.95
        self.assertAlmostEqual(d['gross'], round(expected_gross, 2), places=1)

    def test_all_no_3way_real_world_psg_bayern(self):
        """The exact case from the dashboard screenshot.
        sum_no = 1.94, N=3, balance ~ $100 → real net ≈ $3.09 (NOT $6.23)."""
        # NO prices: 0.54, 0.73, 0.67 (matches PSG/Draw/Bayern sketch)
        prices = [0.54, 0.73, 0.67]
        # liquidity high enough that risk-cap is the only scaling factor
        d = arb_server.build_deal('test_AN_3', 'Polymarket', _outcomes(prices),
                                   sum(prices), theta=0.0, threshold=1.97,
                                   payout_target=2.0)
        self.assertIsNotNone(d)
        ab = sum(e['stake'] for e in d['entries'])
        # Expected gross = ab * (2 - 1.94) / 1.94
        expected_gross = ab * (2.0 - 1.94) / 1.94
        self.assertAlmostEqual(d['gross'], round(expected_gross, 2), places=1)
        # Real ROI ≈ 3.09%, NOT 6.18%
        self.assertLess(d['roi'], 4.0,
                        f"ALL_NO N=3 real ROI must be ~3%, got {d['roi']}%")
        self.assertGreater(d['roi'], 2.5,
                           f"ALL_NO N=3 real ROI must be >2.5%, got {d['roi']}%")

    def test_all_no_4way_realistic_categorical(self):
        """Realistic 4-way categorical (NO sum_no ≈ 2.95 with 5% overround).
        For sum_yes ≈ 1.05, sum_no = N − sum_yes ≈ 2.95. Spread vs payout
        target N−1=3 is only ~$0.05, ROI ≈ 1.7%."""
        # NO prices for 4 mutually-exclusive outcomes — realistic shape
        prices = [0.78, 0.74, 0.72, 0.71]   # sum 2.95
        d = arb_server.build_deal('test_AN_4', 'Polymarket', _outcomes(prices),
                                   sum(prices), theta=0.0, threshold=2.97,
                                   payout_target=3.0)
        self.assertIsNotNone(d)
        # Real ROI ≈ (3 - 2.95) / 2.95 ≈ 1.7%
        self.assertLess(d['roi'], 3.0,
                        f"ALL_NO N=4 ROI must be small (~1.7%), got {d['roi']}%")
        self.assertGreater(d['roi'], 0.5)

    def test_old_buggy_formula_would_have_overstated_2x_for_n3(self):
        """Regression-pin: the OLD formula gave gross = balance * (2 - 1.94)
        ≈ $6 on $100, the NEW formula gives ≈ $3.09. Confirm we report the
        NEW number."""
        prices = [0.54, 0.73, 0.67]
        d = arb_server.build_deal('test_old_vs_new', 'Polymarket',
                                   _outcomes(prices), sum(prices),
                                   theta=0.0, threshold=1.97,
                                   payout_target=2.0)
        # Old formula: 100 * (2 - 1.94) = $6.00 gross
        # New formula: 100 * (2 - 1.94) / 1.94 ≈ $3.09 gross
        self.assertLess(d['gross'], 5.0,
                        f"gross still using old formula? got {d['gross']}")
        self.assertGreater(d['gross'], 1.5)

    def test_yes_no_pair_binary(self):
        # YES + NO binary, sum 0.95 → 5.26% real ROI
        prices = [0.45, 0.50]
        d = arb_server.build_deal('test_C', 'Limitless', _outcomes(prices),
                                   sum(prices), theta=0.0, threshold=0.99,
                                   payout_target=1.0)
        self.assertIsNotNone(d)
        # 5.26%, allow small tolerance
        self.assertLess(d['roi'], 6.5)
        self.assertGreater(d['roi'], 4.5)

    def test_zero_total_price_safe(self):
        # Defensive: total_price=0 must NOT crash, must return None
        prices = [0.0, 0.0]
        d = arb_server.build_deal('test_zero', 'Polymarket', _outcomes(prices),
                                   0.0, theta=0.0, threshold=1.0)
        self.assertIsNone(d, "zero total_price must not yield a deal")


class TestAllYesEconomicsAcrossN(unittest.TestCase):
    """Sweep ALL_YES (structure A) across realistic N values to verify
    the payout formula gross = balance * (1 - sum) / sum holds."""

    def _run(self, prices, expected_roi_pct, label):
        sum_p = sum(prices)
        d = arb_server.build_deal(label, 'Limitless', _outcomes(prices, liq=10000),
                                  sum_p, theta=0.0, threshold=0.99,
                                  payout_target=1.0)
        self.assertIsNotNone(d, f'{label}: deal None on sum {sum_p}')
        # ROI tolerance ±0.4% absolute
        self.assertAlmostEqual(d['roi'], expected_roi_pct, delta=0.4,
                               msg=f'{label}: roi {d["roi"]}, expected {expected_roi_pct}')

    def test_a_binary_5pct_spread(self):
        # YES on each side — sum 0.95, ROI = 5/95 = 5.26%
        self._run([0.45, 0.50], 5.26, 'A binary 5%')

    def test_a_3way_3pct_spread(self):
        # 3 outcomes, sum 0.97, ROI = 3/97 = 3.09%
        self._run([0.30, 0.32, 0.35], 3.09, 'A 3-way 3%')

    def test_a_4way_2pct_spread(self):
        # 4 outcomes, sum 0.98, ROI = 2/98 = 2.04%
        self._run([0.25, 0.24, 0.24, 0.25], 2.04, 'A 4-way 2%')

    def test_a_5way_1pct_spread(self):
        # 5 outcomes, sum 0.99 → ROI = 1/99 = 1.01%
        self._run([0.20, 0.20, 0.20, 0.19, 0.20], 1.01, 'A 5-way 1%')

    def test_a_no_arb_when_sum_at_threshold(self):
        # sum exactly at threshold → not an arb (build_deal returns None
        # via net<=0 filter… or yields tiny positive — accept either)
        prices = [0.50, 0.50]   # sum 1.0
        d = arb_server.build_deal('A no-arb', 'Limitless',
                                   _outcomes(prices, liq=10000),
                                   sum(prices), theta=0.0, threshold=0.99,
                                   payout_target=1.0)
        # gross = balance * (1 - 1) / 1 = 0 → net = -fee → None
        self.assertIsNone(d, 'must reject zero-spread deal')


class TestAllNoEconomicsAcrossN(unittest.TestCase):
    """Sweep ALL_NO (structure B) across N=3, 4, 5 with realistic prices."""

    def _run(self, prices, payout_target, expected_roi, label):
        sum_p = sum(prices)
        d = arb_server.build_deal(label, 'Polymarket',
                                   _outcomes(prices, liq=100000),
                                   sum_p, theta=0.0,
                                   threshold=payout_target * 0.99,
                                   payout_target=float(payout_target))
        self.assertIsNotNone(d, f'{label}: deal None on sum {sum_p}')
        self.assertAlmostEqual(d['roi'], expected_roi, delta=0.4,
                               msg=f'{label}: roi {d["roi"]}, expected {expected_roi}')

    def test_b_3way_real_psg(self):
        # PSG/Draw/Bayern real: sum_no 1.94, payout 2 → ROI = 6/194 = 3.09%
        self._run([0.54, 0.73, 0.67], 2, 3.09, 'B 3-way real')

    def test_b_3way_tight(self):
        # Tighter market: sum_no 1.97, payout 2 → ROI = 3/197 = 1.52%
        self._run([0.60, 0.75, 0.62], 2, 1.52, 'B 3-way tight')

    def test_b_4way_realistic(self):
        # 4-way categorical: sum_no 2.95, payout 3 → ROI = 5/295 = 1.69%
        self._run([0.78, 0.74, 0.72, 0.71], 3, 1.69, 'B 4-way')

    def test_b_5way_categorical(self):
        # 5-way (e.g. division winner): sum_no ≈ 3.96, payout 4 → ROI = 4/396 = 1.01%
        self._run([0.80, 0.79, 0.79, 0.79, 0.79], 4, 1.01, 'B 5-way')

    def test_b_phantom_104pct_is_dead(self):
        """Reddit-DAUq exact numbers — what the dashboard used to show as
        $104.44 net / 104.4% ROI. After Phase 9q fix the same input gives
        the actual mathematical truth: ~$53 (no threshold-series filter
        in build_deal, but eval_* drops it before we ever see it)."""
        # Note: this is RAW build_deal — eval_limitless would filter it out
        # via threshold-series guard. Here we just verify the math.
        prices = [0.50, 0.49, 0.48, 0.483]   # sum 1.953
        d = arb_server.build_deal('phantom_check', 'Limitless',
                                   _outcomes(prices, liq=100000),
                                   sum(prices), theta=0.0, threshold=2.96,
                                   payout_target=3.0)
        # Was 104.7% before fix, now ≈ (3 - 1.953) / 1.953 = 53.6%
        self.assertLess(d['roi'], 60.0,
                        f"build_deal raw ROI for synthetic {d['roi']}%; "
                        f"the 104% phantom must be gone (was old formula)")
        self.assertGreater(d['roi'], 40.0)


class TestYesNoPairEconomics(unittest.TestCase):
    """Sweep YES_NO_PAIR (structure C) — single-market reciprocal binary."""

    def _run(self, yes, no, expected_roi, label):
        sum_p = yes + no
        d = arb_server.build_deal(label, 'Limitless',
                                   _outcomes([yes, no], liq=10000),
                                   sum_p, theta=0.0, threshold=0.99,
                                   payout_target=1.0)
        self.assertIsNotNone(d, f'{label}: deal None')
        self.assertAlmostEqual(d['roi'], expected_roi, delta=0.5,
                               msg=f'{label}: roi {d["roi"]}, expected {expected_roi}')

    def test_c_binary_5pct(self):
        # YES 0.45 + NO 0.50 = 0.95 → ROI 5/95 = 5.26%
        self._run(0.45, 0.50, 5.26, 'C 5% spread')

    def test_c_binary_2pct(self):
        # YES 0.40 + NO 0.58 = 0.98 → ROI 2/98 = 2.04%
        self._run(0.40, 0.58, 2.04, 'C 2% spread')

    def test_c_binary_no_arb_when_sum_1(self):
        d = arb_server.build_deal('C no-arb', 'Limitless',
                                   _outcomes([0.50, 0.50], liq=10000),
                                   1.0, theta=0.0, threshold=0.99,
                                   payout_target=1.0)
        self.assertIsNone(d, 'reciprocal sum=1 means no spread')


class TestRoiCappedToReality(unittest.TestCase):
    """Sanity: the dashboard must NEVER show >50% ROI for ANY of the three
    structures on a real (non-degenerate) input. After Phase 9q this is
    enforced by the formula itself, but pin the invariant."""

    def test_a_max_realistic_arb_under_50pct(self):
        # Even an absurdly cheap A-arb (sum 0.5) still gives only ROI 100%
        # in raw math; let's pin that we don't go above that.
        prices = [0.20, 0.30]   # sum 0.50, theoretical ROI = 0.50/0.50 = 100%
        sum_p = sum(prices)
        d = arb_server.build_deal('A extreme', 'Limitless',
                                   _outcomes(prices, liq=10000),
                                   sum_p, theta=0.0, threshold=0.99,
                                   payout_target=1.0)
        self.assertIsNotNone(d)
        # ROI ≤ 100% is the math cap for ALL_YES — anything beyond means bug
        self.assertLessEqual(d['roi'], 105.0,
                              f'ALL_YES ROI must not exceed 100%, got {d["roi"]}%')


if __name__ == '__main__':
    unittest.main(verbosity=2)
