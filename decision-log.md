# Decision Log — Team AI-043 / PS-160

1. Python 3.10+ chosen over Java 17+ — faster, clearer live edits in the viva; stdlib covers all file formats. Locked 17 Nov 2026.
2. All money and rates as Decimal — binary float cannot be trusted to the rupee.
3. Engines are pure functions over the shared models — every BR is testable in isolation.
4. Command parser isolated in M1 (ingestion) — the brief fixes no command format, so the format must be cheap to change.
5. Availability compared as an exact fraction, not a rounded percentage — the 99.9000 / 99.8999 edge case depends on it.
6. Median fill uses reduced samples only — BR-01 defines the sample value as max(in, out) before any fill.
7. Overage cap applied to the priced Rs amount, not to Mbps — the brief caps the charge at 2x commit price.
8. Total-GB volume per sample = reduced Mbps x 300 s / 8 / 1000 (decimal GB); free 1,000 GB shared across regions in proportion to regional volume — assumption, pending organiser clarification.
9. Overage cap measured against the commit actually charged this month (pro-rated if applicable) — assumption, pending clarification.
10. Nothing hard-coded per customer/region/month — 3 of 8 evaluation customers are hidden.
11. Tests written with stdlib unittest so the suite runs with one command and zero installs (pytest can also collect it).
12. Invoice is never mutated after issue — corrections are a re-run, keeping outputs reproducible.
