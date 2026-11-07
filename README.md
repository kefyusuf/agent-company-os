# AI Company OS

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

**PRs #1, #2 and #3 are merged into `main`.** The project remains a local deterministic fixture with incomplete authorization, agent runtime, human approval and evidence verification. See [project status](docs/PROJECT_STATUS.md) and [PR history](https://github.com/kefyusuf/agent-company-os/pulls?q=is%3Apr+is%3Amerged).

## Source of truth

- `contracts/core-rc1.yaml` — machine-readable *candidate* contract manifest.
- `docs/architecture/m4.3-contract-freeze.md` — architecture decisions, invariants, conformance matrix.
- `docs/implementation/m4.4-vertical-slice.md` — first implementation slice and acceptance gates.

The architecture and the M5.1/M5.2 fixture code are now merged into `main` (PRs #1–#3). For exact proof and omissions, see [project status](docs/PROJECT_STATUS.md).

## Intended first vertical slice

Human Owner → CEO → CTO → Engineering Manager → Backend Engineer → temporary Research Subagent, followed by evidence-backed reports and reviews moving back up to the owner.

An agent's **identity and work history are durable**; individual model/tool executions are **temporary**. Authorization, reporting and audit decisions belong to deterministic application logic rather than model output.

## Working rules

1. Preserve the M4.3 contract as the baseline; any semantic change requires an explicit revision and review.
2. Implement the smallest end-to-end, testable path before building a UI or adding more industries.
3. Do not enable autonomous production deployments, payments, unrestricted customer-data access, or other high-impact external actions.
4. Keep changes reviewable in focused pull requests. Include evidence of tests run and clearly report anything not tested.

**Project stage:** M5.2 deterministic workflow merged; M5.3 authorization boundary pending. The local suite reports 46 passing tests (2026-11-08), with no GitHub CI verification. The `Actor` type is caller asserted and must not be exposed to untrusted clients.

[Next: M5.3 Authorization Boundary](docs/implementation/m5.3-authorization-boundary.md)
