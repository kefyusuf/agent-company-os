# AI Company OS

[![ci](https://github.com/kefyusuf/agent-company-os/actions/workflows/ci.yml/badge.svg)](https://github.com/kefyusuf/agent-company-os/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

An **industry-agnostic, provider-independent AI company operating system** built around persistent agent identities, explicit organizational hierarchy, delegated work, evidence-based reporting, durable institutional memory, and policy-controlled execution.

## Current status

**Fixture-tested persistence and workflow prototype — not a production or autonomous agent runtime.**

The canonical design baseline is **`ai-company-os.core@0.1.0-rc1`** (M4.3). Its examples and test scenarios are specifications, **not passing tests**.

## Project history and status

The initial M1–M4 design work was developed in conversation and is now **reconstructed and documented** in this repository. These are design records, not historical source-code commits or working implementations.

- [Architecture index (M1–M4)](docs/architecture/README.md)
- [Milestone ledger and PR merge order](docs/PROJECT_STATUS.md)
- [M4.3 canonical contract candidate](docs/architecture/m4.3-contract-freeze.md)
- [M4.4 minimum implementation slice](docs/implementation/m4.4-vertical-slice.md)

**PRs #1–#4, #6, #7, #9 and #11–#17 are merged into `main`.** The project remains a local deterministic fixture with an in-process authorization boundary; agent runtime, external side effects and evidence verification remain out of scope. See [project status](docs/PROJECT_STATUS.md) and [PR history](https://github.com/kefyusuf/agent-company-os/pulls?q=is%3Apr+is%3Amerged).

## Source of truth

- `contracts/core-rc1.yaml` — machine-readable *candidate* contract manifest.
- `docs/architecture/m4.3-contract-freeze.md` — architecture decisions, invariants, conformance matrix.
- `docs/implementation/m4.4-vertical-slice.md` — first implementation slice and acceptance gates.

The architecture and the M5.1/M5.2/M5.3-A/B1/B2 fixture code are now merged into `main` (PRs #1–#3, #6, #7, #9). For exact proof and omissions, see [project status](docs/PROJECT_STATUS.md).

## Intended first vertical slice

Human Owner → CEO → CTO → Engineering Manager → Backend Engineer → temporary Research Subagent, followed by evidence-backed reports and reviews moving back up to the owner.

An agent's **identity and work history are durable**; individual model/tool executions are **temporary**. Authorization, reporting and audit decisions belong to deterministic application logic rather than model output.

## Working rules

1. Preserve the M4.3 contract as the baseline; any semantic change requires an explicit revision and review.
2. Implement the smallest end-to-end, testable path before building a UI or adding more industries.
3. Do not enable autonomous production deployments, payments, unrestricted customer-data access, or other high-impact external actions.
4. Keep changes reviewable in focused pull requests. Include evidence of tests run and clearly report anything not tested.

**Project stage:** M5.2 workflow, M5.3-A command gateway, M5.3-B1 policy evaluator, M5.3-B2 SQLite policy gateway, M5.3-C (trusted host attestation, one-time approvals, budget reservation, review lineage), M5.4 D-PR1 (OperationAttempt ledger + scripted adapter seam), D-PR2 (reconciliation command for unknown attempts), D-PR3 (human reconciliation decisions, reissue-as-new-attempt, pause guards) and M5.5 T-PR1 (two-phase commit-then-dispatch + recovery command, fixture-level) are merged. The suite reports 261 passing tests with the offline verifier floor at 261 (2026-11-27); **GitHub Actions CI enforces the suite, lint, type check, coverage and the offline verifier on every push and pull request**. The `Actor` type is caller asserted and must not be exposed to untrusted clients.

[Next: M5.5 real transport review — pre-provider gate drafted with owner-approved decisions; external side effects remain NO-GO until every gate has evidence AND an explicit owner GO](docs/implementation/m5.5-transport-review.md)
