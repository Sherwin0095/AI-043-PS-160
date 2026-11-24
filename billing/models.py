"""Domain models shared by all modules of the PS-160 billing engine.

Team AI-043. This file is the shared vocabulary of the project: every
module imports these types instead of passing raw dicts around, so a
field name means the same thing in ingestion, pricing, SLA and invoicing.

Money and rates are always ``Decimal`` -- never ``float`` -- because the
final invoices must match the expected outputs to the rupee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum


def D(value) -> Decimal:
    """Convert an int/str/Decimal to Decimal safely.

    Floats go through ``str()`` first so that ``D(0.1)`` is exactly 0.1
    and not the binary approximation 0.1000000000000000055511151231257827.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class BillingModel(str, Enum):
    """The two billing models a contract can use (core requirement 1)."""

    P95 = "P95"              # 95th-percentile rate vs CDR (BR-02..BR-06)
    TOTAL_GB = "TOTAL_GB"    # per-GB transferred (BR-05)


@dataclass(frozen=True)
class Contract:
    """A customer contract, loaded from the contracts file (JSON).

    Only the fields the pricing module needs are required here; the SLA
    target is carried as well because the same file supplies it.
    """

    customer_id: str
    cdr_mbps: Decimal                 # Committed Data Rate, Mbps
    commit_price_rs: Decimal          # full monthly commit price, Rs
    billing_model: BillingModel
    contract_start: date
    sla_target_pct: Decimal = Decimal("99.9")
    # Optional per-region rate overrides from the contract file.
    # If a rate is absent, the BR-05 default in pricing.py is used.
    overage_rate_by_region: dict = field(default_factory=dict)
    gb_rate_by_region: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.customer_id:
            raise ValueError("contract: customer_id must not be empty")
        if D(self.cdr_mbps) < 0:
            raise ValueError(
                f"contract {self.customer_id}: CDR must not be negative"
            )
        if D(self.commit_price_rs) < 0:
            raise ValueError(
                f"contract {self.customer_id}: commit price must not be negative"
            )


@dataclass(frozen=True)
class PercentileResult:
    """Output of the percentile engine (M2, BR-01/BR-02).

    The pricing module consumes this; it does not compute percentiles.
    """

    global_p95_mbps: Decimal
    p95_by_region: dict               # region name -> Decimal Mbps
    highest_p95_region: str | None = None   # if None, pricing derives it


@dataclass(frozen=True)
class PriorMonth:
    """One month of P95 history, used only for the step-up rule (BR-08)."""

    month: str                        # "YYYY-MM"
    p95_mbps: Decimal
    cdr_mbps: Decimal
    step_up_applied: bool = False     # a STEP_UP was already issued for it


@dataclass
class PricingResult:
    """Everything the pricing module (M3) hands to invoicing (M5).

    Amounts are Rs as Decimal, quantised to paise (2 dp, half-up).
    """

    customer_id: str
    billing_model: BillingModel
    # Commit (BR-03, BR-07)
    commit_full_rs: Decimal
    commit_charged_rs: Decimal
    billable_days: int
    days_in_month: int
    pro_rated: bool
    # P95-model overage (BR-03..BR-06); zero for the TOTAL_GB model
    cdr_mbps: Decimal                 # never pro-rated (BR-07)
    p95_mbps: Decimal = Decimal("0")
    raw_overage_mbps: Decimal = Decimal("0")
    chargeable_overage_mbps: Decimal = Decimal("0")
    overage_region: str | None = None
    overage_rate_rs_per_mbps: Decimal = Decimal("0")
    # TOTAL_GB model (BR-05); zero/empty for the P95 model
    total_gb: Decimal = Decimal("0")
    chargeable_gb: Decimal = Decimal("0")
    # Charges after the cap (BR-06)
    overage_charge_before_cap_rs: Decimal = Decimal("0")
    overage_charge_rs: Decimal = Decimal("0")
    overage_capped: bool = False
    overage_waived_rs: Decimal = Decimal("0")
    # Step-up for next month (BR-08)
    step_up: bool = False
    next_month_cdr_mbps: Decimal | None = None
    next_month_commit_price_rs: Decimal | None = None

    @property
    def flags(self) -> list:
        """Flags the invoice must print, in the brief's exact wording."""
        out = []
        if self.overage_capped:
            out.append("OVERAGE_CAPPED")
        if self.step_up:
            out.append("STEP_UP")
        return out
