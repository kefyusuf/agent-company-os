# Project Status & Repository Handoff

**Verified snapshot:** 2026-11-08  
**Repository:** https://github.com/kefyusuf/agent-company-os  
**Default branch:** `main`  
**Status vocabulary:** Designed ≠ Implemented ≠ Locally tested ≠ CI-verified ≠ Production-ready.

## Repository synchronization

The project began with design sessions before this repository was created. M1–M4 historical decisions were **reconstructed and labeled as design records**; they are not verbatim original artifacts. The authoritative M4.3 contract candidate is version `0.1.0-rc1`.

**All three foundational pull requests are now merged into `main`:**

| PR | Merged scope | Final status |
| --- | --- | --- |
| [#1](https://github.com/kefyusuf/agent-company-os/pull/1) | M1–M4 architecture recovery, RC1 manifest, M4.4 plan | **Merged** |
| [#2](https://github.com/kefyusuf/agent-company-os/pull/2) | M5.1 Python/SQLite persistence, tenant-scoped records, fixtures and tests | **Merged** |
| [#3](https://github.com/kefyusuf/agent-company-os/pull/3) | M5.2 bounded task delegation, immutable reports, separate review decisions | **Merged** |

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
| M5.3 | Trusted command authorization boundary | **Next / contract in review**; no authenticated boundary implemented |

## Verification evidence and limits

```sh
python -m unittest discover -s tests -q
python -m compileall -q company_os tests
```

Observed in the isolated local environment on **2026-11-08**: **46/46 unittest cases passed**, plus Python syntax compilation. This result covers the test fixtures and code reviewed in the merged PRs; **GitHub Actions has not run**. It is not full M4.3 invariant coverage or a production security claim.

The M4.3 YAML was successfully parsed and contains 20 invariant definitions, 14 command-guard definitions and six cross-domain scenario definitions. Those counts describe a **design manifest**, not 40 passing conformance cases.

**Still missing / NO-GO for autonomous deployment:**

- Authentication and genuine principal/appointment attestation; `Actor` is currently **caller supplied**.
- Complete default-deny authorization, budgets, task/role/capability policies, human approval binding, and independent review policy.
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

## Rules for next contributions

- Do not add an internet-facing API or allow untrusted callers to construct/submit `Actor` and call `Store` / `Workflows` methods.
- The new authorization boundary must protect **every public command** and recheck at the actual side-effect boundary.
- Keep changes in one canonical repository with a testable PR; mark all unimplemented guarantees honestly.
- No production payments, deployments, customer-data export, unbounded agent execution or self-granted authority.
- Update this ledger and include exact observed test evidence whenever a milestone changes.
