# AI-043-PS-160 — CDN and Bandwidth Billing Engine

Capstone Project (10 SECONDS Placement Training, Christ University)
Team AI-043 · 6BTCSAIML A · Problem Statement PS-160

Builds the monthly billing engine a CDN/ISP finance team runs for every
enterprise customer: 95th-percentile bandwidth billing against a Committed
Data Rate, burst allowance, regional overage pricing, an overage cap, CDR
step-up, and SLA credits (credits themselves live in the SLA module).

**Language: Python 3.10+ (locked, Week 1). No third-party dependencies —
standard library only.**

## Team and module ownership

| Member | Register No. | Modules |
|---|---|---|
| SABHARIMANIVEL B | 2462187 | M1 Ingestion & Validation, M2 Percentile Engine (BR-01, BR-02, BR-12) |
| SHERWIN RICHARD RANJITH | 2462190 | M3 Pricing Engine (BR-03 – BR-08) — `billing/pricing.py` |
| SHOMIK SAHU | 2462191 | M4 SLA & Availability (BR-09, BR-10), M5 Invoicing & Reporting (BR-11) |

## Run the tests (single command)

From this directory:

```
python -m unittest discover -s tests -v
```

The same suite is also collected by pytest if it is installed:

```
python -m pytest
```

## Layout

```
billing/
  models.py    shared domain types (Contract, PercentileResult, PricingResult)
  pricing.py   M3 pricing engine: BR-03 commit, BR-04 burst, BR-05 regional /
               total-GB pricing, BR-06 overage cap, BR-07 pro-rata, BR-08 step-up
tests/
  test_pricing.py   one test class per business rule, hand-computed expectations
```

## Pricing pipeline (order matters)

commit (pro-rated if mid-month start) → raw overage = max(0, P95 − CDR) →
burst allowance removes the free first 10% of CDR → chargeable overage priced
at the rate of the region with the highest P95 (Rs 450 India / 650 Singapore /
800 Europe per Mbps), or total-GB pricing with the first 1,000 GB free →
overage capped at 2× the commit charged (excess waived, `OVERAGE_CAPPED`) →
step-up check: P95 above 150% of CDR for 2 consecutive months raises next
month's CDR to 125% and scales the commit price (`STEP_UP`).

All money is `Decimal`, rounded half-up to paise in exactly one place
(`pricing.money`). SLA credits and GST are applied later by M4/M5 and are
deliberately not part of this module.
