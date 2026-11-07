# Architecture Documentation Index

> **Reconstruction notice:** The M1–M4 stage documents below consolidate architectural decisions made in prior project-design conversations. They are **not verbatim archived conversations**, historical Git commits, or independently validated code. The later **M4.3 contract freeze candidate** takes precedence if wording differs.

| Stage | Source of truth in this repository | Implementation state |
| --- | --- | --- |
| Product charter | [00 — Vision & boundaries](00-project-charter.md) | Design record |
| M1.1–M1.3 | [01 — Organization & Workforce](01-m1-organization-workforce.md) | Design record |
| M2.1–M2.3 | [02 — Work, Memory & Governance](02-m2-work-memory-governance.md) | Design record, some later fixture code |
| M3.1–M3.3 | [03 — Composition & Staffing](03-m3-company-composition.md) | Design record only |
| M4.1–M4.2 | [04 — Consistency & Contract Specification](04-m4-consistency-contracts.md) | Design review |
| M4.3 | [Contract Freeze RC1](m4.3-contract-freeze.md) / [YAML manifest](../../contracts/core-rc1.yaml) | Review candidate, **not executable conformance** |
| M4.4 | [Minimal Vertical Slice Plan](../implementation/m4.4-vertical-slice.md) | Planning |
| M5.1 / M5.2 | [PR #2](https://github.com/kefyusuf/agent-company-os/pull/2) / [PR #3](https://github.com/kefyusuf/agent-company-os/pull/3) | Fixture-only implementation, stacked unmerged PRs |

## Precedence and change protocol

1. Mandatory platform security and tenant isolation invariants.
2. `contracts/core-rc1.yaml` + `m4.3-contract-freeze.md` (candidate normative baseline).
3. M4.4 and accepted, reviewed implementation ADRs, limited to their stated scope.
4. Reconstructed M1–M4 documents as **historical intent**, not override authority.

When a conflict exists, add a documented decision and review rather than silently editing a historic decision to match code. Never silently promote local test results to full conformance.

## Known incomplete history

- The full original illustrative ~47-role software catalog is **not** stored as a verified full dataset.
- Original fine-grained M1/M2 invariants from early conversations are not reproduced verbatim; use the M4.3 canonical register for test requirements.
- Full task/policy schemas and runtime implementation do not yet exist.
- Draft PRs are not visible on `main` until merged.

[Current project status and merge sequence](../PROJECT_STATUS.md).
