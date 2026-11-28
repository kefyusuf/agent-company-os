# Changelog

Notable changes are summarized here following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The authoritative honest-status ledger is [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md); this file only summarizes milestone history per version and never carries status claims of its own.

## [Unreleased]

### Added
- M5.4 D-PR3 — human reconciliation decisions (`operation_reconciliations`), reissue-as-new-attempt consuming the decision atomically, FIX-10 guard refinement (consumed reissue releases, `abandon` blocks forever), FLOW-04 pause split (PR #27).
- M5.5 real transport review — pre-provider gate: threat-model delta (T1–T10), gate checklist (G-T1–G-T10) and owner-approved decisions; docs-only, no transport code (PR #29).
- M5.5 T-PR1 — two-phase commit-then-dispatch (`authorize_dispatch` → effect → `settle_dispatch`), `operation.recover` resolving mid-flight attempts from adapter status evidence only (never re-dispatching), `authorized→unknown` transition, pre-call transport identity (`transport_key`, G-T4) and a fixture-only crash-window simulation.
- M5.5 T-PR2 — typed `ExternalAdapter` protocol, `ExternalHttpTransport` with pre-network allowlist/TLS/credential denials, owner-decision failure mapping (4xx→`failed`; 408/429/5xx/timeout→`unknown`), redirect denial, and a localhost fake-provider contract harness proving G-T3/T4/T5/T6/T7/T9.
- M5.5 T-PR3b — resolve-and-validate IP-pinning (`TRANSPORT_DNS_DENIED` fail-closed for non-loopback endpoints, connection pinned to a validated address with TLS SNI/cert verification on the original hostname), provider decision recorded (web fetch of fixed public URLs), revocable GO mechanism designed, and G-T10 independent evidence via a Mimosa deep scan (transport slice clean).

### Changed
- P3 norm hardening: `mypy` gate added for the runtime package, `ruff` line-length tightened 160 → 120, CI tool versions pinned, Dependabot enabled for GitHub Actions and pip, this CHANGELOG introduced.

## [0.1.0] - 2026-11-16

### Added
- M1–M4 architecture recovery records, M4.3 RC1 contract manifest and M4.4 vertical-slice plan (PRs #1–#4).
- M5.1 SQLite persistence foundation with tenant-scoped records and planning guards (PR #2).
- M5.2 bounded task delegation, immutable reports and separated review decisions (PR #3).
- M5.3-A trusted fixture command gateway with a default-deny command allowlist (PR #6).
- M5.3-B1 pure versioned DENY/PERMIT/APPROVAL_REQUIRED policy evaluator (PR #7).
- M5.3-B2 trusted SQLite policy resolver, gateway binding and replay protection (PR #9).
- M5.3-C trusted host attestation, one-time approval consumption, budget reservation and review lineage (PRs #14–#17).
- M5.4 D-PR1 OperationAttempt ledger, scripted adapter seam and the FIX-10 no-blind-redispatch guard (PR #20).
- M5.4 D-PR2 reconciliation command for unknown attempts with terminal `unresolved` (PR #23).
- Industry-norm hardening: CI workflow with coverage gate, ruff lint, PR evidence template, CODEOWNERS and SECURITY.md with the false-positive registry (PR #21).
- Repo norm closeout: Apache-2.0 LICENSE, PEP 621 `pyproject.toml`, document sync (PR #25).

### Security
- Default-deny authorization, parameterized SQL everywhere, fail-closed command paths, and a reviewed, auditable static-analysis false-positive registry instead of silent suppressions.

[Unreleased]: https://github.com/kefyusuf/agent-company-os/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kefyusuf/agent-company-os/releases/tag/v0.1.0
