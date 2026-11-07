# M2 — Work, Delegation, Memory & Governance

**Source:** Consolidated architecture decisions from M2.1–M2.3 design conversations.  
**Status:** Architecture reconstruction, **not** proof of implementation.  
**Later corrections:** M4.1–M4.3 are normative for conflicting field/state definitions.

## M2.1 — Work & Delegation Protocol

### Canonical records

| Record | Meaning |
| --- | --- |
| `Task` | Durable work unit: objective, acceptance criteria, owner, worker, reviewer, state, parent |
| `Delegation` | Explicit authorized parent-to-target handoff with narrowed scope |
| `AgentRun` | One execution attempt, checkpoints/leases, recoverable history |
| `Report` | Submitted report **version** with source/evidence references |
| `Review` | Authoritative assessment of an exact report/deliverable revision |
| `Evidence` | Provenance and integrity-linked support for claims |
| `ServiceRequest` | Cross-department request, **not** a management/delegation edge |
| `Approval` | Specific action authorization, **not** a work review |
| `AuditEvent` | Immutable/append-only recorded decision or state transition |

A Task has `requested_by`, exactly one accountable appointment, an assigned appointment, and specific recipient/reviewer references when needed. The task tree is distinct from the company reporting graph; parent Task accountability does not disappear upon delegation.

**Canonical Task states:** `draft`, `ready`, `assigned`, `in_progress`, `blocked`, `submitted`, `under_review`, `changes_requested`, `accepted`, `closed`, `failed`, `cancelled`. `failed` is a terminal **task decision**, not merely a failed model run; `blocked` is not terminal.

**Acceptance rule:** Task acceptance requires deliverables, applicable acceptance criteria, accepted mandatory child tasks (or authorized scope waiver), required independent verification and approvals. `Report.accepted` as a user-facing notion is derived from a separate `Review` decision, and must not auto-accept the Task. `accepted` and `closed` remain distinct.

**Delegation:** Proposed → policy evaluation → offered → accepted/declined → child executes/submits → fulfilled upon correct child acceptance. Additional revoked/expired/terminated outcomes apply. Delegation is bounded by actor appointment, task scope, resources, spending budget, sub-agent fan-out, execution count and time. Unauthorized direct cross-appointment assignment is denied until an accepted delegation exists.

**Cross-department work:** A Product/Engineering Manager may request analysis from Finance; the Finance manager decides how to allocate their own team. Requester is **not** the requested department's direct manager.

**Reports:** Submitted versions immutable; every report/review records actual author and active appointment; change requests produce a new version. Upper-level managers summarize subordinate reports with source refs, and CEO provides the Owner with an evidence-based executive report. `report_delivered` and `owner_approved` are different events.

**Recovery:** Leases, checkpoints and idempotency prevent duplicate unsafe work. An external side-effect whose result is unknown must be reconciled before retry. Do not claim the full recovery protocol has been implemented.

### Reference business scenario

Owner requests investigation of an e-commerce order/API performance issue (only a demo scenario). CEO → CTO → Engineering Manager → Backend Engineer → temporary research subagent. Research reports to Backend; Backend reports to Manager; independent QA and upper-level reviews when policy requires. CEO delivers traceable outcome.

## M2.2 — Persistent Memory & Evidence

Four conceptual layers must not be conflated:

1. **Canonical operational records:** Tasks, decisions, policies, reviews, appointments.
2. **Artifacts / evidence:** Source files, test outputs, business documents, external references; integrity/revision metadata where possible.
3. **Institutional memory:** Derived, scoped knowledge, with source links, validity and lifecycle.
4. **Assembled runtime context:** Temporary, bounded prompt/retrieval view; never a new source of truth.

### Memory scope

`company`, `department`, `project`, `agent`, `task`, `run`, with explicit access policy. Company/customer confidentiality boundaries apply **before retrieval**.

### Memory classification and lifecycle

- Categories: **episodic**, **semantic**, **procedural**.
- Proposed candidate → reviewed → active/rejected; later stale/superseded/revoked/archived.
- Verification status is **separate** from lifecycle. A useful active hypothesis can still be unverified; acceptance of its containing report does not verify it.
- Records include: tenant/scope, title/statement/type, author identity and appointment, source Task/Report/Evidence refs, verification actor/method/status, validity/review date, version, access/retention policy.
- Do not generate unsupported numeric truth/confidence scores.

### Authorized retrieval

Authenticate actor → resolve active appointment/task → filter by tenant/scope/access policy → retrieve relevant data → check provenance, validity, conflicts and freshness → assemble bounded context with citations and uncertainty. Retrieved text, tool output and external webpages are **untrusted data**, not an instruction source.

Share information by authorized MemoryQuery, ArtifactReference, ReportReference, InformationRequest, KnowledgePromotion; not unrestricted cross-agent transcript access.

### Handoff

Agent replacement/model changes preserve canonical records. Handoff contains outstanding tasks, verified progress, next steps, known issues, report/evidence refs and new recipient's access scope. Agent-private observations do **not** automatically become company/public knowledge.

## M2.3 — Authorization, Governance & Approval

### Action decision

Effective permission requires conjunction of actor identity, active appointment, role/capability constraints, current Task/Delegation, resource/tenant boundary, company state, higher-level policy, budget and possibly approval.

An agent's prompt, title or tool availability does **not** create authority. Enforce at the **tool/command boundary**, using deterministic checks and **default deny**. Recheck immediately before sensitive side effects.

Decision vocabulary: `PERMIT`, `DENY`, `APPROVAL_REQUIRED`. Explicit hard prohibition is not magically lifted by an ordinary approval.

### Reference action risk levels

| Level | Typical use | Control principle |
| --- | --- | --- |
| L0 | Authorized reading/research | Data-access check, never universal access |
| L1 | Scoped reversible work | Task+role+resource scope |
| L2 | Shared-system change | Policy gate, reviews or delegated authority |
| L3 | High-impact financial/external/legal/security action | Specific human approval plus other checks |

Classification is context-based; the same operation may have different risk depending on environment/data.

### Approval and external execution

- `Approval` binds the **specific action, resource, payload/revision digest, policy version, approver, expiry and allowed usage**.
- Independent review cannot be satisfied by an agent acting under two appointments, or by its controlled subagent.
- `Approval.approved` is permission to attempt, **not proof of execution**. A consumed approval maps to one `OperationAttempt`; the latter carries success/failure/unknown/reconciliation status.
- Policy or appointment changes can invalidate pending permissions. Do not retry irreversible actions blindly.
- Budgets intersect across company, department, task, agent and delegation, including cost, tool calls, fan-out and concurrent runs.
- Governance policy changes require versioning, diff/impact analysis and authorized higher-level approval. No self-escalation by CEO or subagents.
- Deny on missing authority, stale approval, foreign tenant, or absent required audit; no sensitive secrets in prompts/logs.

### Enforcement truthfulness

The M5.1/M5.2 prototypes do **not** implement full actor authentication, policy evaluation, approval verification, budget/rate enforcement, external OperationAttempt or memory access. Some deterministic checks exist only in a trusted local fixture; production execution remains NO-GO.

## Outstanding work

Exact field-level schemas, transition guard/waiver matrix, complete ServiceRequest lifecycle, security policy engine, valid external-action identity proof, trusted evidence verification, source retention, concurrent execution/recovery and the full 40-case contract suite.

## Related

- [M1 organizational model](01-m1-organization-workforce.md)
- [M3 composition model](03-m3-company-composition.md)
- [M4 contract corrections](04-m4-consistency-contracts.md)
- [M4.3 manifest](../../contracts/core-rc1.yaml)
