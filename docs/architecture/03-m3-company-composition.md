# M3 — Blueprint Composition, Staffing & Activation

**Source:** Reconstructed decisions from M3.1–M3.3.  
**Status:** Design only; examples are not applied company migrations, active agents, or executable schemas.

## M3.1 — Company Blueprint & Composition

A `CompanyBlueprint` is a **versioned template** of organization and capability needs. It is **not** a company instance, an employee, or a live LLM session.

Conceptual composition:

```text
Shared Role / Function Catalog
    + Core Company Blueprint
    + zero/more Industry Packs
    + Lean/Standard/Extended staffing preset
    + User Customization
 -> Organization Draft
 -> Validated Organization Plan
 -> Owner approval
 -> Company + Positions
 -> Staffing / Readiness
 -> Activation
```

**Common business functions:** executive, accounting/finance, risk/security, people/workforce, operations, legal/compliance, procurement, sales/marketing, customer support. A required **function** does not require a dedicated AI employee; it may be covered by a combined agent, authorized human, or external provider.

**Industry examples:** Software company (Engineering/Product/Delivery); Game studio (Game Design/Production/Engineering); Textile company (Design/Manufacturing/Quality/Logistics). These are **configuration examples**, not separate kernels.

**Staffing modes:** dedicated agent, combined role, human, external provider, unassigned. Unassigned mandatory functions prevent activation, but an optional empty position need not block an otherwise ready company.

**Hybrid packs:** Software + Game may share Finance while keeping separate Engineering and Game Design teams. Merge **functions** by stable function ID; do not auto-merge **positions** merely because titles match (CTO vs Technical Director, multiple Backend Engineers). Policy rights are never unioned by accident.

**Custom roles:** A company-specific role may extend a versioned base role and request additional capabilities/permissions, subject to authorization and conflict checks. Schema naming and exact override legality remained open for the compiler.

## M3.2 — Blueprint Compiler & Validation Contracts

Three logical responsibilities, not three required microservices:

| Responsibility | Meaning |
| --- | --- |
| Company Builder | Natural-language or structured **proposal** (AI permitted) |
| Company Compiler | Deterministically resolve, normalize, merge and emit a plan |
| Provisioner | Apply **approved and validated** plan to durable company records |

Compiler inputs: pinned Core Blueprint, Industry Pack references, Role Catalog refs, organization preset, explicit customizations, governance policies. Output: a **Compiled Organization Plan** with stable keys, department graph, positions, function coverage, staffing requirements, source provenance, policy references, validation findings and a canonical content digest.

Deterministic steps:

1. Resolve references and **lock versions**.
2. Normalize stable keys and relationship types.
3. Compose functions and role instances using field-specific merge rules.
4. Build the reporting graph separately from collaboration/service-request relationships.
5. Validate, report all actionable conflicts.
6. Emit versioned draft + provenance without creating model sessions.

Validation layers:

| ID | Check |
| --- | --- |
| V1 | Schema and typed fields |
| V2 | Versioned catalog references |
| V3 | Acyclic, connected CEO-root organization with single direct manager per non-CEO position |
| V4 | Design-level and operational-level function coverage |
| V5 | Staffing identity, capability, appointment and capacity fit |
| V6 | Permissions, policy, independence and tenant boundaries |

Output findings may be `error`, `warning`, or `advisory`. A structurally valid draft is not automatically operationally ready. Ambiguous package merge, missing roles, reporting cycles or privilege override do **not** silently resolve by AI guess.

**Organization ChangeSet:** Dry-run → validate → impact diff (tasks/owners/policies/roles/capacity) → owner approval → guarded apply → audit. Existing company updates use expected organization revision; past reports retain historical authors and managerial appointments. Reallocation of open tasks requires explicit handoff.

Blueprint updates are opt-in and **do not silently mutate existing companies**.

## M3.3 — Company Provisioning, Staffing, Runtime & Lifecycle

### Provisioning separation

`CompiledOrganizationPlan` + owner approval matching its digest → creation of Company, OrganizationRevision, Positions and permitted records. **Provisioning is not activation** and **creation of AgentIdentity is not StartRun**. Transactional/idempotent behavior should prevent duplicates and half-activated companies.

### Durable employee vs runtime

```text
Position (job slot)
   <--- Appointment (time-bound, company-scoped) ---> AgentIdentity (durable)
                                              |
                                         RuntimeBinding (config)
                                              |
                                         AgentRun (ephemeral)
```

An agent can fill compatible positions through separate appointments; default is one primary active assignment per position. Human/external work coverage is recorded without fabricating an AI identity.

Capacity has organizational workload, concurrency and resource-budget dimensions. Capability fit is an assessed requirement, not an LLM self-description.

Model/provider changes may change runtime and run history but cannot replace the durable worker or rewrite the employee's past output. Credentials exist only in controlled runtime; not in prompts or MemoryRecord.

### Company readiness

**Company Readiness**: CEO, required business functions, organization, records, auth, memory and audit foundations are prepared.

**Capability Readiness**: A specific requested service has actual qualified staff, tools and budget. An active company need not be capable of performing every service.

`activation_ready` is a **validation result**, not a Company lifecycle state.

### Company lifecycle

`provisioning` → `inactive` or `provisioning_failed`; `inactive` → `active` only through approval/readiness; `active` ↔ `paused`; security-driven `suspended` requires authorized recovery; `archived` requires lifecycle/retention policy.

- **Pause:** Stop scheduling new operational runs; checkpoint or safely quiesce ongoing runs.
- **Suspend:** Security block/revocation with restricted recovery access.
- **Archive:** Preserve or delete records according to lawful retention and policy; not indiscriminate destruction.
- External in-flight side effects are reconciled independently; pause cannot guarantee instant rollback of a payment/deploy/order.

### Staff replacement / handoff

Keep old AgentIdentity and historic task/report authors. End old Appointment, restrict old access, create new Appointment, and explicitly transfer outstanding work with source/evidence refs, accountable manager and authorization. New agent sees only authorized company/project information; private old-agent notes do not transfer wholesale.

## Reference acceptance cases

- Software, Game and Textile each compile under the **same** organization schema.
- Combining Software + Game preserves independent positions and one common accounting function.
- Invalid role, double manager, graph cycle, missing mandatory coverage or permission escalation blocks activation.
- Identical input versions produce the same normalized plan and digest.
- Blueprint upgrade yields a proposed diff, not a silent live migration.
- Owner-approved provisioning is idempotent; model sessions do not start automatically.
- Agent swap retains history, revokes old rights, and explicitly reassigns open work.

## Not implemented in M3

No native Company Builder UI, general compiler, Blueprint schema engine, provisioning service, real activation readiness, model runtime profile, employee capability assessment, full versioned change migration or multi-sector live company.

## Related

- [M1 roles](01-m1-organization-workforce.md)
- [M2 governance](02-m2-work-memory-governance.md)
- [M4 contract freeze](m4.3-contract-freeze.md)
