"""Tests for the pricing engine (M3, BR-03..BR-08).

Run from the repository root with a single command -- either:
    python -m unittest discover -s tests -v     (stdlib only, no installs)
    python -m pytest                            (if pytest is available)

Every test is named after the business rule it proves. Expected values
are hand-computed from the brief, not copied from the implementation.
"""

import unittest
from datetime import date
from decimal import Decimal

from billing.models import BillingModel, Contract, PercentileResult, PriorMonth
from billing.pricing import (
    apply_overage_cap,
    chargeable_overage_mbps,
    evaluate_step_up,
    highest_p95_region,
    overage_rate_rs_per_mbps,
    price_month,
    price_total_gb,
    pro_rated_commit,
    raw_overage_mbps,
    total_gb_from_reduced_samples,
)

D = Decimal


def p95_contract(**overrides):
    base = dict(
        customer_id="CUST-1",
        cdr_mbps=D("100"),
        commit_price_rs=D("20000"),
        billing_model=BillingModel.P95,
        contract_start=date(2026, 1, 1),
    )
    base.update(overrides)
    return Contract(**base)


def percentile(global_p95, by_region=None):
    return PercentileResult(
        global_p95_mbps=D(global_p95),
        p95_by_region=by_region or {"India": D(global_p95)},
    )


class TestBR03CommitAndRawOverage(unittest.TestCase):
    def test_br03_p95_below_cdr_means_zero_overage_but_full_commit(self):
        r = price_month(p95_contract(), 2026, 3, percentile("80"))
        self.assertEqual(r.raw_overage_mbps, D("0"))
        self.assertEqual(r.overage_charge_rs, D("0.00"))
        self.assertEqual(r.commit_charged_rs, D("20000.00"))  # paid regardless

    def test_br03_raw_overage_is_p95_minus_cdr(self):
        self.assertEqual(raw_overage_mbps(D("130"), D("100")), D("30"))
        self.assertEqual(raw_overage_mbps(D("90"), D("100")), D("0"))


class TestBR04BurstAllowance(unittest.TestCase):
    def test_br04_first_10_percent_of_cdr_is_free(self):
        # P95 130, CDR 100: raw overage 30, free burst 10, chargeable 20
        self.assertEqual(chargeable_overage_mbps(D("130"), D("100")), D("20"))

    def test_br04_p95_exactly_110_percent_of_cdr_charges_nothing(self):
        # Brief edge case: the burst boundary itself is free.
        self.assertEqual(chargeable_overage_mbps(D("110"), D("100")), D("0"))

    def test_br04_p95_just_above_boundary_charges_the_excess_only(self):
        self.assertEqual(chargeable_overage_mbps(D("111"), D("100")), D("1"))


class TestBR05RegionalPricing(unittest.TestCase):
    def test_br05_rates_by_region(self):
        self.assertEqual(overage_rate_rs_per_mbps("India"), D("450"))
        self.assertEqual(overage_rate_rs_per_mbps("Singapore"), D("650"))
        self.assertEqual(overage_rate_rs_per_mbps("Europe"), D("800"))

    def test_br05_unknown_region_is_an_error_not_a_silent_zero(self):
        with self.assertRaises(ValueError):
            overage_rate_rs_per_mbps("Atlantis")

    def test_br05_highest_p95_region_sets_the_rate_not_traffic_share(self):
        # Brief edge case: most traffic in India, but Europe's P95 is the
        # highest, so ALL chargeable overage is priced at Europe's rate.
        by_region = {"India": D("120"), "Singapore": D("115"), "Europe": D("130")}
        self.assertEqual(highest_p95_region(by_region), "Europe")
        r = price_month(p95_contract(), 2026, 3, percentile("130", by_region))
        self.assertEqual(r.overage_region, "Europe")
        self.assertEqual(r.overage_rate_rs_per_mbps, D("800"))
        self.assertEqual(r.chargeable_overage_mbps, D("20"))
        self.assertEqual(r.overage_charge_rs, D("16000.00"))  # 20 x 800

    def test_br05_contract_rate_overrides_brief_default(self):
        c = p95_contract(overage_rate_by_region={"India": D("500")})
        self.assertEqual(overage_rate_rs_per_mbps("India", c), D("500"))
        self.assertEqual(overage_rate_rs_per_mbps("Europe", c), D("800"))

    def test_br05_total_gb_first_1000_gb_free_india(self):
        charge, total, chargeable = price_total_gb({"India": D("1500")})
        self.assertEqual(total, D("1500"))
        self.assertEqual(chargeable, D("500"))
        self.assertEqual(charge, D("2250.00"))  # 500 GB x Rs 4.50

    def test_br05_total_gb_under_free_allowance_charges_nothing(self):
        charge, _, chargeable = price_total_gb({"Europe": D("900")})
        self.assertEqual(chargeable, D("0"))
        self.assertEqual(charge, D("0.00"))

    def test_br05_total_gb_non_india_rate_is_6_per_gb(self):
        charge, _, _ = price_total_gb({"Singapore": D("2000")})
        self.assertEqual(charge, D("6000.00"))  # 1000 chargeable GB x Rs 6

    def test_br05_total_gb_conversion_from_samples(self):
        # One reduced sample of 100 Mbps for 5 min = 100 x 300 / 8 / 1000 GB
        self.assertEqual(total_gb_from_reduced_samples([D("100")]), D("3.75"))
        self.assertEqual(
            total_gb_from_reduced_samples([D("100"), D("100")]), D("7.5"))


class TestBR06OverageCap(unittest.TestCase):
    def test_br06_charge_below_cap_is_unchanged(self):
        final, capped, waived = apply_overage_cap(D("15000"), D("20000"))
        self.assertEqual((final, capped, waived), (D("15000.00"), False, D("0.00")))

    def test_br06_charge_above_twice_commit_is_capped_and_flagged(self):
        final, capped, waived = apply_overage_cap(D("50000"), D("20000"))
        self.assertEqual(final, D("40000.00"))   # 2 x commit
        self.assertTrue(capped)
        self.assertEqual(waived, D("10000.00"))

    def test_br06_cap_applies_inside_price_month_and_sets_flag(self):
        # P95 400 on CDR 100, India: chargeable 290 x 450 = 130,500,
        # capped at 2 x 20,000 = 40,000.
        r = price_month(p95_contract(), 2026, 3, percentile("400"))
        self.assertEqual(r.overage_charge_before_cap_rs, D("130500.00"))
        self.assertEqual(r.overage_charge_rs, D("40000.00"))
        self.assertTrue(r.overage_capped)
        self.assertIn("OVERAGE_CAPPED", r.flags)


class TestBR07ProRata(unittest.TestCase):
    def test_br07_contract_starting_on_the_31st_of_a_31_day_month(self):
        # Brief edge case: exactly 1 billable day.
        charged, days, dim = pro_rated_commit(
            D("31000"), date(2026, 1, 31), 2026, 1)
        self.assertEqual((charged, days, dim), (D("1000.00"), 1, 31))

    def test_br07_mid_month_start_pro_rates_by_calendar_days(self):
        # Start Jan 16 -> Jan 16..31 inclusive = 16 billable days.
        charged, days, _ = pro_rated_commit(D("31000"), date(2026, 1, 16), 2026, 1)
        self.assertEqual(days, 16)
        self.assertEqual(charged, D("16000.00"))

    def test_br07_contract_started_before_the_month_bills_in_full(self):
        charged, days, dim = pro_rated_commit(
            D("20000"), date(2025, 12, 20), 2026, 1)
        self.assertEqual((charged, days), (D("20000.00"), dim))

    def test_br07_contract_starting_next_month_bills_nothing(self):
        charged, days, _ = pro_rated_commit(D("20000"), date(2026, 2, 1), 2026, 1)
        self.assertEqual((charged, days), (D("0.00"), 0))

    def test_br07_cdr_is_not_pro_rated(self):
        # Start on Jan 31: commit is 1/31, but the full CDR of 100 still
        # sets the burst ceiling, so P95 110 charges nothing.
        c = p95_contract(commit_price_rs=D("31000"),
                         contract_start=date(2026, 1, 31))
        r = price_month(c, 2026, 1, percentile("110"))
        self.assertEqual(r.commit_charged_rs, D("1000.00"))
        self.assertEqual(r.cdr_mbps, D("100"))
        self.assertEqual(r.chargeable_overage_mbps, D("0"))


class TestBR08StepUp(unittest.TestCase):
    def test_br08_two_consecutive_months_above_150_percent_steps_up(self):
        history = [PriorMonth("2026-01", D("160"), D("100"))]
        step, new_cdr, new_commit = evaluate_step_up(
            history, "2026-02", D("170"), D("100"), D("20000"))
        self.assertTrue(step)
        self.assertEqual(new_cdr, D("125"))            # 125% of old CDR
        self.assertEqual(new_commit, D("25000.00"))    # scales proportionally

    def test_br08_only_one_month_above_does_not_step_up(self):
        history = [PriorMonth("2026-01", D("120"), D("100"))]
        step, _, _ = evaluate_step_up(
            history, "2026-02", D("170"), D("100"), D("20000"))
        self.assertFalse(step)

    def test_br08_exactly_150_percent_does_not_trigger(self):
        # "Exceeds 150%" is strict.
        history = [PriorMonth("2026-01", D("150"), D("100"))]
        step, _, _ = evaluate_step_up(
            history, "2026-02", D("150"), D("100"), D("20000"))
        self.assertFalse(step)

    def test_br08_no_double_step_up_when_already_applied(self):
        # Brief edge case: the pair already produced a STEP_UP.
        history = [PriorMonth("2026-01", D("160"), D("100"),
                              step_up_applied=True)]
        step, _, _ = evaluate_step_up(
            history, "2026-02", D("170"), D("100"), D("20000"))
        self.assertFalse(step)

    def test_br08_step_up_is_printed_on_the_result_flags(self):
        history = [PriorMonth("2026-02", D("160"), D("100"))]
        r = price_month(p95_contract(), 2026, 3, percentile("170"),
                        prior_history=history)
        self.assertTrue(r.step_up)
        self.assertIn("STEP_UP", r.flags)
        self.assertEqual(r.next_month_cdr_mbps, D("125"))
        # ...but this month's invoice figures are unchanged by it.
        self.assertEqual(r.cdr_mbps, D("100"))


class TestValidationAndModels(unittest.TestCase):
    def test_negative_p95_is_rejected(self):
        with self.assertRaises(ValueError):
            price_month(p95_contract(), 2026, 3, percentile("-5"))

    def test_negative_commit_in_contract_is_rejected(self):
        with self.assertRaises(ValueError):
            p95_contract(commit_price_rs=D("-1"))

    def test_p95_model_without_percentile_result_is_rejected(self):
        with self.assertRaises(ValueError):
            price_month(p95_contract(), 2026, 3, percentile=None)

    def test_total_gb_model_end_to_end(self):
        c = p95_contract(billing_model=BillingModel.TOTAL_GB)
        r = price_month(c, 2026, 3, gb_by_region={"India": D("1500")})
        self.assertEqual(r.total_gb, D("1500"))
        self.assertEqual(r.chargeable_gb, D("500"))
        self.assertEqual(r.overage_charge_rs, D("2250.00"))
        self.assertEqual(r.commit_charged_rs, D("20000.00"))


if __name__ == "__main__":
    unittest.main()
