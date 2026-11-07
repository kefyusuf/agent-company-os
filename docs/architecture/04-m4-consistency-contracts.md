# M4 — Contract Consistency, Executable Contracts & Freeze

**Status:** Design reconciliation. `v0.1.0-rc1` is a **candidate** contract, not a fully executable schema or proven application.  
**Canonical files:** [M4.3 freeze](m4.3-contract-freeze.md) and [manifest](../../contracts/core-rc1.yaml).  
**Sources:** M4.1, M4.2, M4.3 discussions; restated, not verbatim.

## M4.1 — Consistency & invariants

Eight conflict families were reviewed:

| ID | Concern | Resolved architectural intent |
| --- | --- | --- |
| CR-01 | Task completion vs report acceptance | AgentRun completion, Report/Review acceptance and Task acceptance are distinct |
| CR-02 | Manager/employee replacement during ongoing work | Historical relationships immutable; open work transfers explicitly |
| CR-03 | Self-review with multiple roles | Enforce independence on real AgentIdentity and applicable delegation chain |
| CR-04 | Authorization changes during execution | Revalidate at command/tool execution; revoke stale appointments/approvals |
| CR-05 | Report acceptance vs verified truth | Independent evidence and Memory verification lifecycle |
| CR-06 | Company active vs each capability ready | Separate readiness of whole company from specific service |
| CR-07 | Mandatory child task cancelled/failed | Parent acceptance blocked unless explicit authorized scope waiver |
| CR-08 | Company pause vs in-flight external side effect | Independent OperationAttempt state and reconciliation |

The **20 cross-cutting invariants** include identity persistence, scoped authority, one accountable appointment, accepted mandatory child work, report/task separation, independent reviews, no privilege escalation, immutable historic owners, no memory-as-truth, tenant isolation, pause behavior, durable audit and retention. Exact IDs and their conformance mapping are in the canonical manifest, not duplicated as competing sources here.

## M4.2 — Command and state contracts

Every command carries actor/appointment (or valid human/system principal), company/task context, command ID, expected revision where applicable and idempotency key. A command requesting completion **does not** become completion merely because an LLM claimed it.

### Core conceptual state vocabulary

- **Task:** draft, ready, assigned, in_progress, blocked, submitted, under_review, changes_requested, accepted, closed, failed, cancelled.
- **Delegation:** proposed, offered, accepted, fulfilled; rejected, declined, revoked, expired, terminated outcomes.
- **Report:** editable draft, then immutable submitted versions.
- **Review:** specific report version + accepted / changes_requested / rejected decision.
- **Approval:** draft, pending, approved, denied, cancelled, expired, revoked, consumed.
- **OperationAttempt:** planned, authorized, dispatched, succeeded, failed, unknown, reconciling, unresolved.
- **Company:** provisioning, provisioning_failed, inactive, active, paused, suspended, archived.

Exact transition guards are **not fully enumerated in executable code**, and many M5.1/M5.2 states remain intentionally unavailable.

### Operation boundaries

```text
Authenticated command → validate schema / expected revision
 → authorize identity, appointment, company, task, policy and resource
 → validate transition and evidence guards
 → atomic internal state + audit + receipt
 → optional safely tracked external action
```

Never assume exactly-once external execution. An unknown financial/production side effect requires reconciliation before rerun.

## M4.3 — RC1 decision corrections

The freeze candidate specifies ten fixes (FIX-01…FIX-10). Core consequences:

- `Review` is authoritative for submitted report-version acceptance.
- `ReassignTask` and `TransferAccountability` are **different operations**.
- `activation_ready` is a **readiness result**, not Company state.
- Permanent `AgentIdentity` belongs to one company; LLM/runtime can be shared without identity sharing.
- `ServiceRequest` across departments is not equivalent to a management subtask.
- Humans/external providers are not faked as `AgentIdentity`.
- Approval consumed != operation successful.
- One active primary appointment per position in the initial model.
- Report acceptance != Memory verification.
- Network timeout != external operation failed.

See [full M4.3 decision table](m4.3-contract-freeze.md) for exact wording.

## Contract tests and quality gates

RC1 lists **20 positive + 20 negative** invariant cases, **14 command guard descriptions** and **6 cross-domain flows**. Counts describe **definitions**, not passing tests.

Seven implementation gates:

| Gate | Meaning | Current proof |
| --- | --- | --- |
| G1 | Exact schema fields/references | Partial SQLite subset only |
| G2 | State transition guards | Partial deterministic subset only |
| G3 | Auth + isolation + policy enforcement | Not passed; trusted fixture only |
| G4 | 40 canonical conformance cases | Not passed as full suite |
| G5 | Six cross-domain flows | Full suite not run; deterministic record fixture is partial |
| G6 | Crash recovery / idempotency / unknown external outcome | Not passed; basic restart checks only |
| G7 | Executive report to accepted source evidence | Source reference links partially tested, verification incomplete |

**No-go:** No live external agent/tool actions or production autonomy until auth, policy, provenance, recovery and acceptance criteria meet applicable gates.

## M4.4 — First vertical slice

The intentionally narrow reference workflow:

```text
Human Owner
  ↓
CEO → CTO → Engineering Manager → Backend Engineer
                                      ↓
                              Temporary Research Subagent
                                      ↓
                   immutable reports and evidence references
                                      ↑
                        reviewed management reports
                                      ↑
                                    CEO
                                      ↓
                                Human Owner
```

Use one company, safe fixed tasks, deterministic runtime before real LLM execution. Provider/framework selection remains a separate decision.

The initial implementation decision for local validation was Python 3.11+ / SQLite / standard-library unittest (see stacked PR #2 ADR); **not a commitment to the final product stack**.

## Remaining contract debt

- Complete machine-validatable schema package, nullability, references and migration rules.
- Complete transition/waiver matrix, approval fingerprint matching and real authentication.
- Independent factual verification of Evidence and source revisions.
- Policy precedence, budget and capability evaluation.
- Full owner handoff/approval, company activation readiness.
- Complete 40-case suite, all six flows, recovery and high-impact action protections.

## Implementation connection

- PR #1: contract docs/manifest and M4.4 plan — **draft** as of 2026-11-08.
- PR #2: SQLite core, reported **16 local tests passed** — **draft**.
- PR #3: delegation/reports, reported **45 total local tests passed** — **draft**.
- No recorded CI success and **none of the above are merged into main** at this documentation snapshot.

For current status, inspect the GitHub PRs directly and [PROJECT_STATUS.md](../PROJECT_STATUS.md).
