"""Pricing engine -- Module M3, business rules BR-03 to BR-08.

Owner: SHERWIN RICHARD RANJITH (2462190), Team AI-043, PS-160.

This module prices one customer for one month. It does NOT compute
percentiles (M2), availability or credits (M4), or invoices/GST (M5).
It is a set of pure functions over the shared models, plus one
orchestrator, ``price_month``, which runs the rules in the order the
brief requires:

    commit (BR-03, pro-rata BR-07)
      -> raw overage (BR-03)
      -> burst allowance (BR-04)
      -> price at the highest-P95 region's rate / total-GB price (BR-05)
      -> overage cap (BR-06)
      -> CDR step-up check for next month (BR-08)

Why this order (the viva question): the burst allowance removes free
usage *before* pricing, so free Mbps are never priced; the cap is a
ceiling on a rupee *charge*, so it can only be applied *after* pricing
-- capping Mbps first would give a different answer whenever the
applicable regional rate is not the one the cap assumed.

Assumptions that are still open with the organisers (design doc,
Appendix B) are marked ASSUMPTION below and isolated in one place each,
so a clarification changes one function, not the whole module:
  A1. The overage cap (BR-06) and the step-up commit scaling (BR-08)
      use the commit actually charged / the full monthly commit
      respectively -- see the functions.
  A2. Total-GB volume uses reduced samples: GB = Mbps x 300 s / 8 / 1000
      (decimal GB), and the free 1,000 GB is allocated across regions
      in proportion to each region's share of the month's volume.
"""

from __future__ import annotations

import calendar
from decimal import Decimal, ROUND_HALF_UP

from .models import (
    BillingModel,
    Contract,
    D,
    PercentileResult,
    PricingResult,
    PriorMonth,
)

# ---------------------------------------------------------------------------
# Rule constants. Every number that comes from the brief lives here, once,
# so a rule change in the viva is a one-line edit in an obvious place.
# ---------------------------------------------------------------------------

# BR-05: overage rate per Mbps, by region (Rs)
OVERAGE_RATE_RS_PER_MBPS = {
    "India": Decimal("450"),
    "Singapore": Decimal("650"),
    "Europe": Decimal("800"),
}

# BR-05: total-GB model -- Rs per GB (India vs everywhere else)
GB_RATE_INDIA_RS = Decimal("4.50")
GB_RATE_OTHER_RS = Decimal("6")
FREE_GB_PER_MONTH = Decimal("1000")

# BR-04: the first 10% of CDR worth of overage is free
BURST_ALLOWANCE_FRACTION = Decimal("0.10")

# BR-06: overage charge may not exceed 2x the commit price
OVERAGE_CAP_MULTIPLIER = Decimal("2")

# BR-08: P95 above 150% of CDR for 2 consecutive months steps the CDR
# up to 125% of the old CDR, commit price scaling proportionally
STEP_UP_THRESHOLD_FRACTION = Decimal("1.50")
STEP_UP_CDR_FRACTION = Decimal("1.25")

PAISE = Decimal("0.01")
SECONDS_PER_SAMPLE = Decimal("300")   # 5-minute samples


def money(value) -> Decimal:
    """Round a rupee amount to paise, half-up -- the module's only rounding."""
    return D(value).quantize(PAISE, rounding=ROUND_HALF_UP)


def _require_non_negative(name: str, value) -> Decimal:
    value = D(value)
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {value}")
    return value


# ---------------------------------------------------------------------------
# BR-07 -- mid-month contract start: commit pro-rated by calendar days.
# CDR is NOT pro-rated and the burst allowance is NOT pro-rated; both are
# handled by simply never scaling them anywhere in this module.
# ---------------------------------------------------------------------------

def pro_rated_commit(commit_price_rs, contract_start, year: int, month: int):
    """Return (charged_rs, billable_days, days_in_month) for BR-07.

    Billable days count the start day itself: a contract starting on the
    31st of a 31-day month has exactly 1 billable day (brief edge case).
    A contract that starts after this month is not billed this month.
    """
    commit_price_rs = _require_non_negative("commit price", commit_price_rs)
    days_in_month = calendar.monthrange(year, month)[1]
    start_month = (contract_start.year, contract_start.month)

    if start_month < (year, month):
        # Started in an earlier month: the whole month is billable.
        return money(commit_price_rs), days_in_month, days_in_month
    if start_month > (year, month):
        # Has not started yet this month: nothing is billable.
        return Decimal("0.00"), 0, days_in_month

    # Starts inside this month: bill from the start day, inclusive.
    billable_days = days_in_month - contract_start.day + 1
    charged = money(commit_price_rs * billable_days / days_in_month)
    return charged, billable_days, days_in_month


# ---------------------------------------------------------------------------
# BR-03 -- commit is paid regardless of usage;
# raw overage = max(0, P95 - CDR).
# ---------------------------------------------------------------------------

def raw_overage_mbps(p95_mbps, cdr_mbps) -> Decimal:
    p95_mbps = _require_non_negative("P95", p95_mbps)
    cdr_mbps = _require_non_negative("CDR", cdr_mbps)
    return max(Decimal("0"), p95_mbps - cdr_mbps)


# ---------------------------------------------------------------------------
# BR-04 -- burst allowance: overage up to 10% of CDR is free.
# Chargeable overage = max(0, P95 - 1.10 x CDR), so a P95 of exactly
# 110% of CDR charges nothing (brief edge case).
# ---------------------------------------------------------------------------

def chargeable_overage_mbps(p95_mbps, cdr_mbps) -> Decimal:
    p95_mbps = _require_non_negative("P95", p95_mbps)
    cdr_mbps = _require_non_negative("CDR", cdr_mbps)
    free_ceiling = cdr_mbps * (Decimal("1") + BURST_ALLOWANCE_FRACTION)
    return max(Decimal("0"), p95_mbps - free_ceiling)


def highest_p95_region(p95_by_region: dict) -> str:
    """The region whose P95 is highest (BR-04 picks its rate).

    Ties are broken by region name so the result never depends on the
    order rows happened to arrive in the sample file.
    """
    if not p95_by_region:
        raise ValueError("p95_by_region must not be empty")
    return max(sorted(p95_by_region), key=lambda r: D(p95_by_region[r]))


# ---------------------------------------------------------------------------
# BR-05 -- regional pricing.
# ---------------------------------------------------------------------------

def overage_rate_rs_per_mbps(region: str, contract: Contract | None = None) -> Decimal:
    """Overage rate for a region: contract override first, else BR-05."""
    if contract is not None and region in (contract.overage_rate_by_region or {}):
        return D(contract.overage_rate_by_region[region])
    if region not in OVERAGE_RATE_RS_PER_MBPS:
        raise ValueError(f"unknown region for overage pricing: {region!r}")
    return OVERAGE_RATE_RS_PER_MBPS[region]


def gb_rate_rs(region: str, contract: Contract | None = None) -> Decimal:
    """Per-GB rate for the total-GB model (BR-05)."""
    if contract is not None and region in (contract.gb_rate_by_region or {}):
        return D(contract.gb_rate_by_region[region])
    return GB_RATE_INDIA_RS if region == "India" else GB_RATE_OTHER_RS


def total_gb_from_reduced_samples(reduced_samples_mbps) -> Decimal:
    """Convert reduced 5-minute samples to total GB (ASSUMPTION A2).

    Each sample is Mbps held for 300 seconds:
    GB per sample = Mbps x 300 / 8 bits-per-byte / 1000 MB-per-GB.
    """
    total_mbps_samples = sum((D(s) for s in reduced_samples_mbps), Decimal("0"))
    return total_mbps_samples * SECONDS_PER_SAMPLE / Decimal("8") / Decimal("1000")


def price_total_gb(gb_by_region: dict, contract: Contract | None = None):
    """Price the total-GB model. Returns (charge_rs, total_gb, chargeable_gb).

    The first 1,000 GB in the month is free (BR-05). ASSUMPTION A2: the
    free allowance is shared across regions in proportion to each
    region's volume, then each region's remaining GB is priced at that
    region's rate.
    """
    gb_by_region = {r: _require_non_negative(f"GB for {r}", v)
                     for r, v in gb_by_region.items()}
    total_gb = sum(gb_by_region.values(), Decimal("0"))
    chargeable_gb = max(Decimal("0"), total_gb - FREE_GB_PER_MONTH)
    if total_gb == 0 or chargeable_gb == 0:
        return Decimal("0.00"), total_gb, chargeable_gb

    charge = Decimal("0")
    for region, gb in gb_by_region.items():
        region_chargeable = chargeable_gb * gb / total_gb
        charge += region_chargeable * gb_rate_rs(region, contract)
    return money(charge), total_gb, chargeable_gb


# ---------------------------------------------------------------------------
# BR-06 -- overage cap: the overage charge may not exceed 2x the commit
# price. The excess is waived; the invoice prints OVERAGE_CAPPED.
# ASSUMPTION A1: the cap is 2x the commit actually charged this month
# (the pro-rated amount in a mid-month-start month).
# ---------------------------------------------------------------------------

def apply_overage_cap(overage_charge_rs, commit_charged_rs):
    """Return (final_charge_rs, capped: bool, waived_rs)."""
    overage_charge_rs = _require_non_negative("overage charge", overage_charge_rs)
    commit_charged_rs = _require_non_negative("commit charged", commit_charged_rs)
    cap = money(commit_charged_rs * OVERAGE_CAP_MULTIPLIER)
    if overage_charge_rs <= cap:
        return money(overage_charge_rs), False, Decimal("0.00")
    return cap, True, money(overage_charge_rs - cap)


# ---------------------------------------------------------------------------
# BR-08 -- CDR step-up. If P95 exceeds 150% of CDR for 2 consecutive
# months, NEXT month's CDR becomes 125% of the old CDR and the commit
# price scales proportionally. "Exceeds" is strict: exactly 150% does
# not trigger it. A pair of months that already produced a STEP_UP does
# not trigger a second one (brief edge case: a second consecutive month
# above 150% ending in a month with STEP_UP already applied).
# ---------------------------------------------------------------------------

def evaluate_step_up(history, current_month: str, current_p95_mbps,
                     current_cdr_mbps, current_commit_price_rs):
    """Return (step_up: bool, next_cdr_mbps | None, next_commit_rs | None).

    ``history`` is the chronological list of PriorMonth for the months
    before the current one (the brief supplies the prior 2 months).
    """
    current_p95_mbps = _require_non_negative("current P95", current_p95_mbps)
    current_cdr_mbps = _require_non_negative("current CDR", current_cdr_mbps)
    current_commit_price_rs = _require_non_negative(
        "current commit price", current_commit_price_rs)

    sequence = list(history) + [
        PriorMonth(month=current_month, p95_mbps=current_p95_mbps,
                   cdr_mbps=current_cdr_mbps, step_up_applied=False)
    ]
    if len(sequence) < 2:
        return False, None, None

    previous, current = sequence[-2], sequence[-1]
    if previous.step_up_applied or current.step_up_applied:
        return False, None, None

    def exceeds(month_entry) -> bool:
        return D(month_entry.p95_mbps) > (
            D(month_entry.cdr_mbps) * STEP_UP_THRESHOLD_FRACTION)

    if exceeds(previous) and exceeds(current):
        next_cdr = D(current_cdr_mbps) * STEP_UP_CDR_FRACTION
        next_commit = money(current_commit_price_rs * STEP_UP_CDR_FRACTION)
        return True, next_cdr, next_commit
    return False, None, None


# ---------------------------------------------------------------------------
# Orchestrator -- price one customer for one month.
# ---------------------------------------------------------------------------

def price_month(contract: Contract, year: int, month: int,
                percentile: PercentileResult | None = None,
                prior_history=(),
                gb_by_region: dict | None = None) -> PricingResult:
    """Run BR-03..BR-08 for one (customer, month) and return a PricingResult.

    P95-model contracts need ``percentile`` (from M2). TOTAL_GB contracts
    need ``gb_by_region`` (also derivable via total_gb_from_reduced_samples).
    SLA credits are NOT subtracted here -- that is M4/M5's job (BR-09..11);
    this module's overage_charge_rs is the amount invoicing starts from.
    """
    cdr = D(contract.cdr_mbps)
    commit_full = D(contract.commit_price_rs)

    # BR-07 first: it decides the commit this month, which BR-06's cap
    # is measured against. CDR itself is never scaled.
    commit_charged, billable_days, days_in_month = pro_rated_commit(
        commit_full, contract.contract_start, year, month)

    result = PricingResult(
        customer_id=contract.customer_id,
        billing_model=contract.billing_model,
        commit_full_rs=money(commit_full),
        commit_charged_rs=commit_charged,
        billable_days=billable_days,
        days_in_month=days_in_month,
        pro_rated=(billable_days not in (0, days_in_month)),
        cdr_mbps=cdr,
    )

    if contract.billing_model is BillingModel.P95:
        if percentile is None:
            raise ValueError(
                f"{contract.customer_id}: P95 billing needs a PercentileResult")
        p95 = _require_non_negative("P95", percentile.global_p95_mbps)
        region = percentile.highest_p95_region or highest_p95_region(
            percentile.p95_by_region)
        rate = overage_rate_rs_per_mbps(region, contract)
        chargeable = chargeable_overage_mbps(p95, cdr)

        result.p95_mbps = p95
        result.raw_overage_mbps = raw_overage_mbps(p95, cdr)       # BR-03
        result.chargeable_overage_mbps = chargeable                # BR-04
        result.overage_region = region
        result.overage_rate_rs_per_mbps = rate
        charge_before_cap = money(chargeable * rate)               # BR-05

        # BR-08 is evaluated on the full commit price: the step-up sets
        # next month's contract values, it does not change this invoice.
        step_up, next_cdr, next_commit = evaluate_step_up(
            prior_history, f"{year:04d}-{month:02d}", p95, cdr, commit_full)
        result.step_up = step_up
        result.next_month_cdr_mbps = next_cdr
        result.next_month_commit_price_rs = next_commit

    elif contract.billing_model is BillingModel.TOTAL_GB:
        if gb_by_region is None:
            raise ValueError(
                f"{contract.customer_id}: TOTAL_GB billing needs gb_by_region")
        charge_before_cap, total_gb, chargeable_gb = price_total_gb(
            gb_by_region, contract)                                # BR-05
        result.total_gb = total_gb
        result.chargeable_gb = chargeable_gb

    else:  # pragma: no cover - BillingModel is a closed enum
        raise ValueError(f"unknown billing model: {contract.billing_model!r}")

    # BR-06 last among the charges: a ceiling on the priced amount.
    final_charge, capped, waived = apply_overage_cap(
        charge_before_cap, commit_charged)
    result.overage_charge_before_cap_rs = charge_before_cap
    result.overage_charge_rs = final_charge
    result.overage_capped = capped
    result.overage_waived_rs = waived
    return result
