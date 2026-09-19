# DEV-03 primary-source verification notes

Checked 2026-09-16. These notes explain implementation choices; the approved V3 design is unchanged. They are not additional normative acceptance claims.

| Primary source | Application | Qualification limit |
|---|---|---|
| OpenTelemetry Trace SDK — https://opentelemetry.io/docs/specs/otel/trace/sdk/ | Bounded buffered export, independently managed lifecycle, metadata-only projection and stable event IDs. | AMPLAI implements a pinned local projection/spool; no claim of complete SDK or OTLP transport compliance. |
| NIST/SEMATECH e-Handbook, confidence limits for a proportion — https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm | Wilson uncertainty for task-level binary proportions; conservative combined paired difference bounds. | This is an approximate interval procedure, not a universal power guarantee. Repeated trajectories are not independent tasks. |
| SQLite transactions — https://www.sqlite.org/lang_transaction.html | BEGIN IMMEDIATE reservation before executing callbacks; writes serialize around allocation rather than relying on a process-only counter. | A local durable database is not certification of distributed consensus, network filesystem durability, or multi-region recovery. |

Cost benefit uses a deterministic predeclared paired task-mean percentile bootstrap implemented locally, with explicit compute bounds and approximate status. Unknown or unmeasured data is not converted into an efficiency benefit. Holdout protection is currently local ACL/content exposure tracking, not physical isolation from the machine owner. Actual credentials and external enforcement require the separate qualification gates retained in the delivery metadata.
