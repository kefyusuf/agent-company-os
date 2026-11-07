# AI Company OS

An **industry-agnostic, provider-independent AI company operating system** built around persistent agent identities, explicit organizational hierarchy, delegated work, evidence-based reporting, durable institutional memory, and policy-controlled execution.

## Current status

**Architecture / contract candidate — not an implemented runtime.**

The canonical design baseline is **`ai-company-os.core@0.1.0-rc1`** (M4.3). Its examples and test scenarios are specifications, **not passing tests**.

## Source of truth

- `contracts/core-rc1.yaml` — machine-readable *candidate* contract manifest.
- `docs/architecture/m4.3-contract-freeze.md` — architecture decisions, invariants, conformance matrix.
- `docs/implementation/m4.4-vertical-slice.md` — first implementation slice and acceptance gates.

Files listed here are introduced through a bootstrap PR; links become available once that PR is merged.

## Intended first vertical slice

Human Owner → CEO → CTO → Engineering Manager → Backend Engineer → temporary Research Subagent, followed by evidence-backed reports and reviews moving back up to the owner.

An agent's **identity and work history are durable**; individual model/tool executions are **temporary**. Authorization, reporting and audit decisions belong to deterministic application logic rather than model output.

## Working rules

1. Preserve the M4.3 contract as the baseline; any semantic change requires an explicit revision and review.
2. Implement the smallest end-to-end, testable path before building a UI or adding more industries.
3. Do not enable autonomous production deployments, payments, unrestricted customer-data access, or other high-impact external actions.
4. Keep changes reviewable in focused pull requests. Include evidence of tests run and clearly report anything not tested.

**Project stage:** M4.4 planning / repository bootstrap.
