# Project Status & Repository Handoff

**Snapshot:** 2026-11-08. Refresh this file when PRs merge or tests change.  
**Repository:** https://github.com/kefyusuf/agent-company-os  
**Default branch:** `main`  
**Meaning of completion:** "Designed" ≠ "Implemented" ≠ "Tests Passed" ≠ "Merged".

## Why main currently looks empty

The project started with architecture designed in conversation. On GitHub, a README was committed to `main`; the architecture and code were built on **three stacked draft PRs**, **not merged**. M1–M3 source narratives were not yet committed, even though their decisions informed M4.3.

This recovery adds a clear decision inventory to **PR #1** while keeping `main` stable pending review.

## Milestone ledger

| Milestone | Deliverable | Status |
| --- | --- | --- |
| M1.1 | Company / organizational ontology | **Design reconstructed** |
| M1.2 | Role / appointment / capability contracts | **Design reconstructed** |
| M1.3 | Common + industry role catalog | **Reference roles reconstructed**, full ~47-role dataset absent |
| M2.1 | Task, delegation, report and review | **Design**; M5.2 later tests a restricted subset |
| M2.2 | Scoped memory, evidence and provenance | **Design**; basic evidence refs in M5.2, no validated memory service |
| M2.3 | Authorization and human approval | **Design**; fixture checks only, no production policy engine |
| M3.1 | Common/core + industry company blueprints | **Design**, no compiler |
| M3.2 | Deterministic blueprint composition/validation | **Design**, no general compiler |
| M3.3 | Provisioning/staffing/activation and handoff | **Design**, no production activation |
| M4.1 | Cross-domain consistency / 20 invariants | **Design review** |
| M4.2 | State/command/schema specification | **Design review**, not exhaustive executable schemas |
| M4.3 | Contract freeze candidate `0.1.0-rc1` | **PR #1 — unmerged** |
| M4.4 | Minimal vertical-slice specification | **PR #1 — unmerged** |
| M5.1 | SQLite storage, strict task planning checks | **PR #2 — unmerged**; 16 reported local tests |
| M5.2 | Bounded delegation, report/review | **PR #3 — unmerged**; 45 total reported local tests |

### PR chain (do not confuse with main)

| PR | Base branch | Contains | Merge dependency |
| --- | --- | --- | --- |
| [#1](https://github.com/kefyusuf/agent-company-os/pull/1) | `main` | RC1 manifest, M4.4 plan, reconstructed M1–M4 docs | First |
| [#2](https://github.com/kefyusuf/agent-company-os/pull/2) | `docs/m4.4-bootstrap` | Python/SQLite foundation and tests | After #1 |
| [#3](https://github.com/kefyusuf/agent-company-os/pull/3) | `feat/m5.1-persistence-foundation` | Delegation/reporting and tests | After #2 |

When merging a stacked PR: verify upstream merged, retarget the next PR to `main`, inspect the **new diff**, run applicable tests, then merge only when approved. Preserve historical PR evidence.

## What has **not** been proved

- No verified, authenticated production actor boundary; current `Actor` is caller asserted.
- No comprehensive company authorization engine / Approval fingerprint enforcement / budgets.
- No live LLM or autonomous agent tool execution, production payments or deployments.
- No real Company Blueprint compiler, activation readiness or staffing engine.
- No full persistence recovery/lease/OperationAttempt reconciliation.
- No full `20×2` invariant suite, `6` cross-domain flow suite or passing G1–G7 gates.
- The reported **45 local tests** were obtained in earlier isolated validation, **not re-run or confirmed via GitHub CI by this documentation recovery**.

## Immediate next work

1. Review reconstructed historical docs and mark discrepancies as explicit ADRs/edits (do **not** invent prior exact decisions).
2. Review PR #1 and merge if approved; confirm documents appear on `main`.
3. Refresh/retarget PR #2, verify tests and review/merge.
4. Refresh/retarget PR #3, verify tests and review/merge.
5. Proceed with M5.3: authenticated command boundary, concrete policy/acceptance guards, evidence provenance and Owner delivery semantics before connecting an LLM.

## Suggested maintenance rule

Every substantive future milestone should update the appropriate versioned contract/ADR, its runnable tests, this ledger, and PR evidence. Use **one canonical GitHub repository**; avoid off-repo-only design progression.

## Main reading path

[Architecture index](architecture/README.md) → [M4.3 freeze](architecture/m4.3-contract-freeze.md) → [M4.4 slice](implementation/m4.4-vertical-slice.md) → pending PR diffs.
