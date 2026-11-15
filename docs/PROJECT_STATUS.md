# Project Status & Repository Handoff

**Verified snapshot:** 2026-11-15  
**Repository:** https://github.com/kefyusuf/agent-company-os  
**Default branch:** `main`  
**Status vocabulary:** Designed ≠ Implemented ≠ Locally tested ≠ CI-verified ≠ Production-ready.

## Repository synchronization

The project began with design sessions before this repository was created. M1–M4 historical decisions were **reconstructed and labeled as design records**; they are not verbatim original artifacts. The authoritative M4.3 contract candidate is version `0.1.0-rc1`.

**Foundational PRs #1–#4, M5.3-A PR #6, M5.3-B1 PR #7, M5.3-B2 PR #9, the M5.3-C design PR #14 and its implementation PRs #15–#17 have merged into `main`:**

| PR | Merged scope | Final status |
| --- | --- | --- |
| [#1](https://github.com/kefyusuf/agent-company-os/pull/1) | M1–M4 architecture recovery, RC1 manifest, M4.4 plan | **Merged** |
| [#2](https://github.com/kefyusuf/agent-company-os/pull/2) | M5.1 Python/SQLite persistence, tenant-scoped records, fixtures and tests | **Merged** |
| [#3](https://github.com/kefyusuf/agent-company-os/pull/3) | M5.2 bounded task delegation, immutable reports, separate review decisions | **Merged** |
| [#4](https://github.com/kefyusuf/agent-company-os/pull/4) | Verified status and M5.3 safety planning | **Merged** |
| [#6](https://github.com/kefyusuf/agent-company-os/pull/6) | M5.3-A fixture-only trusted command ingress | **Merged** |
| [#7](https://github.com/kefyusuf/agent-company-os/pull/7) | M5.3-B1 pure policy decision evaluator | **Merged**; 49 exact-source local tests verified |
| [#9](https://github.com/kefyusuf/agent-company-os/pull/9) | M5.3-B2 SQLite policy gateway integration | **Merged**; 193/193 full-checkout tests verified (2026-11-14) |
| [#14](https://github.com/kefyusuf/agent-company-os/pull/14) | M5.3-C design (trusted host, approvals, budget, lineage) | **Merged**; owner-approved decisions recorded |
| [#15](https://github.com/kefyusuf/agent-company-os/pull/15) | M5.3-C PR1: atomic budget reservation + contributor lineage | **Merged**; 204/204 checkout tests |
| [#16](https://github.com/kefyusuf/agent-company-os/pull/16) | M5.3-C PR2: one-time approval consumption | **Merged**; 206/206 checkout tests |
| [#17](https://github.com/kefyusuf/agent-company-os/pull/17) | M5.3-C PR3: credential-verified TrustedPrincipalHost | **Merged**; 209/209 checkout tests |

The earlier claim that only a README is on `main` is **obsolete**. The file tree now includes `company_os/store.py`, `company_os/workflow.py`, `tests/`, `docs/` and `contracts/`.

## Milestone ledger

| Milestone | Scope | Evidence / actual state |
| --- | --- | --- |
| M1.1–M1.3 | Organization, roles, capabilities, company-level identity | **Design reconstructed**; not a complete role catalog artifact |
| M2.1 | Task/delegation/report/review protocol | **Design**; restricted subset implemented in M5.2 |
| M2.2 | Memory, evidence, provenance | **Design**; basic evidence refs only, no verified memory service |
| M2.3 | Authorization, human approval, policy gates | **Design only** for full policy engine; fixture actor is caller asserted |
| M3.1 | Core + industry blueprints | **Design** |
| M3.2 | Blueprint compiler & validation | **Design**, compiler not implemented |
| M3.3 | Provisioning, staffing, runtime binding and lifecycle | **Design**, real provisioning/activation not implemented |
| M4.1–M4.3 | Consistency, contracts, 20 invariants | **RC1 candidate**; not full executable conformance |
| M4.4 | Minimal vertical-slice plan | **Documented** |
| M5.1 | SQLite data foundation and planning guards | **Merged**; 17 local tests including empty-fixture regression |
| M5.2 | Delegation + report/review + evidence refs | **Merged**; 29 local tests |
| M5.3-A | Local fixture-only principal session + allowlisted command gateway | **PR #6 merged**; NOT a production authentication boundary |
| M5.3-B1 | Pure versioned DENY/PERMIT/APPROVAL_REQUIRED evaluator | **Merged (PR #7)**; 49 exact-source local tests, not gateway-integrated |
| M5.3-B2 | Trusted SQLite state resolver and gateway policy binding | **Merged (PR #9)**; 52 new tests; full checkout verified 193/193 (2026-11-14) |
| M5.3-C | Trusted host attestation, one-time approvals, budget reservation, review lineage | **Implemented (PRs #14–#17)**; fixture-grade attestation, **NOT production authentication** |

## Verification evidence and limits

```sh
python -m unittest discover -s tests -q
python -m compileall -q company_os tests
```

Observed in the local environment:
- **Merged M5.3-A baseline:** previously reported **71/71 local tests** (46 workflow + 25 gateway tests), before merge PR #6.
- **M5.3-B1 isolated module (2026-11-08, pre-merge):** **49/49 pure policy tests** passed independently on Python 3.13.5; the Git blob SHA of both files matched PR #7 exactly before merge.
- **M5.3-B2 local work directory (2026-11-08, pre-merge):** 101/101 tests passed (49 B1 + 31 SQLite resolver + 21 gateway tests), but this used a lightweight Store/Workflows stand-in and predates later replay/provenance test additions.
- **Merged `main` checkout (2026-11-13):** **120/120 tests** passed via `python -m unittest discover -s tests -q`; `python -m compileall -q company_os tests` clean. Includes the M5.3-B1 module merged by PR #7.
- **M5.3-B2 full checkout (2026-11-14, PR head `2aa569a` + docs-PR #11 merge `80478f1`):** **193/193 tests** passed via `python -m unittest discover -s tests -q`; `python -m compileall -q company_os tests` clean; `scripts/verify.py` **PASS** (AST inventory 193/193, `all_passed: true`). Independent review found no P1/P2 findings; three P3 notes recorded on PR #9.
- **M5.3-C full checkout (2026-11-15, PRs #15–#17):** **209/209 tests** passed via `python -m unittest discover -s tests -q`; `python -m compileall -q company_os tests` clean; `scripts/verify.py` **PASS** (inventory 209/209, `all_passed: true`). Each C PR ran the full suite and raised the verifier floor monotonically (193→204→206→209).
- **GitHub Actions:** not run. These are fixture/pure-unit results, not full M4.3 conformance or production security.

The M4.3 YAML was successfully parsed and contains 20 invariant definitions, 14 command-guard definitions and six cross-domain scenario definitions. Those counts describe a **design manifest**, not 40 passing conformance cases.

**Still missing / NO-GO for autonomous deployment:**

- Production-grade authentication and transport security; direct Store/Workflows still accept **caller-supplied `Actor`** and must never be exposed to untrusted callers. M5.3-C adds a **fixture-grade** credential-verified principal host (`TrustedPrincipalHost`) on top of the B2 policy enforcement, but there is no network transport, IAM, secret store or multi-process deployment — the trusted host and all policy tables stay in-process.
- Production authorization guarantees: signed/verified principal context, external `OperationAttempt` reconciliation, multi-process-safe locking and budget settlement for external actions. Internal default-deny authorization, company-level budget reservation, one-time approval consumption and attested review lineage are now **implemented at fixture level** (M5.3-C); they do not meet production gates.
- Real Owner report review/approval, Company readiness and activation; fixture activation uses test-only direct SQLite state changes.
- Memory verification, actual external evidence integrity/provenance and robust ACL/retention behavior.
- Real LLM + tools integration, secure external OperationAttempt, leases/checkpoints and result reconciliation.
- The full invariant matrix, all six end-to-end flows and G1–G7 release gates.

## Current navigation

1. [Architecture index](architecture/README.md)
2. [M4.3 contract freeze](architecture/m4.3-contract-freeze.md) and [manifest](../contracts/core-rc1.yaml)
3. [M4.4 reference workflow](implementation/m4.4-vertical-slice.md)
4. [M5.1 implementation notes](implementation/m5.1-foundation.md)
5. [M5.2 delegation/report notes](implementation/m5.2-delegation-reporting.md)
6. [M5.3 authorization gate design](implementation/m5.3-authorization-boundary.md)
7. [M5.3-A implementation scope and threats](implementation/m5.3-a-command-gateway.md) — merged PR #6
8. [M5.3-B1 pure policy decisions](implementation/m5.3-b1-policy-decisions.md) — PR #7, merged
9. [M5.3-B2 trusted SQLite snapshot binding](implementation/m5.3-b2-sqlite-policy-gateway.md) — PR #9, merged
10. [M5.3-C trusted host, approvals, budget and lineage](implementation/m5.3-c-trusted-host.md) — PRs #14–#17, merged

## Rules for next contributions

- Do not add an internet-facing API or allow untrusted callers to construct/submit `Actor` and call `Store` / `Workflows` methods.
- The local M5.3-A gateway implements a restricted allowlist; the **real** trusted host must protect every exposed command and recheck authorization at the actual side-effect boundary.
- Keep changes in one canonical repository with a testable PR; run and record the full repository test suite on the combined B1/B2 branch before merge, and mark all unimplemented guarantees honestly.
- No production payments, deployments, customer-data export, unbounded agent execution or self-granted authority.
- Update this ledger and include exact observed test evidence whenever a milestone changes.

**M5.3-B2 follow-up:** [Issue #10 — idempotent replay and live policy revocation](https://github.com/kefyusuf/agent-company-os/issues/10) has a **source fix and regression tests** in PR #9. The replay fix, the full checkout suite (**193/193**, 2026-11-14) and an independent code review are complete; production-authentication NO-GO statements are unchanged.

## Offline verification gate — current B2 branch

`scripts/verify.py` now records test and syntax verification evidence without CI minutes:

```sh
git fetch origin
git switch --detach origin/feat/m5.3-b2-sqlite-policy-gateway
python3 scripts/verify.py --output .local/verification.json
```

**Current evidence (2026-11-14):** A real full checkout at PR head `2aa569a` (plus the docs-PR #11 merge `80478f1`) passed **193/193 tests**, `compileall`, and `scripts/verify.py` with an exact 193/193 inventory match. **PR #9 merge and Issue #10 closure may proceed on this evidence; production/conformance claims are unchanged.**

**2026-11-08 merge update:** PR #7 merged after independent exact Git blob verification and 49/49 policy unit tests. PR #9 was retargeted to `main`; a new guard blocks adopting legacy, un-attested domain receipts as policy-approved gateway commands, with a regression case. **This PR #9 patch and all of its B2 tests are still unverified as one checkout.**

## 2026-11-08 integrity review

PR #9 adds append-only SQLite UPDATE/DELETE restrictions for issued policy versions and successful gateway receipts, plus two new regression tests. A local SQLite 3.46.1 focused smoke test passed **five** immutable/revocation checks. A separate two-connection `BEGIN IMMEDIATE` smoke test passed **two** writer-serialization observations. **These are not full gateway tests.**

The six actual `tests/test_*.py` GitHub files contain **193 declared test methods** (17 + 29 + 25 + 49 + 34 + 39). The offline `scripts/verify.py` now requires all six files, at least 193 declarations, and an exact discovered-count match; this prevents a partial one-test checkout from being marked successful. The full 193-case branch suite was executed on 2026-11-14: **193/193 PASS**; the latest regression tests are verified in the same run.

**PR #9 verification gate satisfied 2026-11-14 (193/193 + verifier PASS + independent review).** No M5.3-C or real external effects.
