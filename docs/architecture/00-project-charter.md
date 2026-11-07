# AI Company OS — Project Charter & Scope

**Status:** Reconstructed architecture decision record (design only).  
**Baseline:** `ai-company-os.core@0.1.0-rc1` in `contracts/core-rc1.yaml`.  
**Provenance:** Consolidated from the preceding project-design conversations; not a verbatim transcript or an implemented feature.

## Product objective

Create an **industry-agnostic AI-operated company**. A human Owner configures the company and gives assignments primarily to a **permanent CEO Agent**. The CEO delegates through real organizational positions; managers delegate to qualified specialists, review subordinate work, and report upward. The Owner receives an evidence-backed executive report and retains reserved decision authority.

This is **not** an e-commerce, Shopify, game, or software-agency-only application. Those industries are examples of a shared organization and work kernel.

## Mandatory product boundaries

1. **Organizational hierarchy:** Acyclic manager/reporting graph, with the human Owner above the CEO. An operational communication or service-request link is not a reporting link.
2. **Persistent workforce:** `AgentIdentity` is not a prompt, a role, an LLM connection, or a run. A persistent agent may fill compatible positions through separate `Appointment` records.
3. **Controlled delegation:** Work belongs to durable `Task` records with distinct responsibility, assignment and review. Subagents can be temporary, scoped and budget-limited.
4. **Evidence and reporting:** Reports and reviews are durable; parent tasks do not become complete merely because a child posted a summary. CEO-level reporting must reference subordinate evidence.
5. **Institutional memory:** Source records and artifacts remain authoritative; derived memory is versioned, provenance-linked and access-controlled.
6. **Authorization:** Permissions are enforced outside the LLM at the action boundary. CEO is a delegated operator, not a substitute for unlimited human authority.
7. **Configurable company:** Common business functions plus industry-specific role packs; users can configure positions, team topology, staffing mode, and authorized policy preferences.
8. **Provider independence:** A model or tool backend may change without erasing agent identity or business history.
9. **Operational restraint:** No uncontrolled deployment, payment, sensitive-data export or other critical external side effect before policy, approval and verification controls exist.
10. **Simplicity:** Favor one coherent domain model and focused interfaces. Separate microservices, broad frontends, embedding stacks and large catalogs are not prerequisites for proving the core workflow.

## Actor and lifecycle distinctions

| Concept | Meaning |
| --- | --- |
| Human Owner | Real person with company-level reserved decisions |
| Company | Durable tenant, policies, lifecycle and organization |
| RoleDefinition | Reusable, versioned job description |
| Position | Company-specific job/organizational node |
| AgentIdentity | Permanent digital worker, company-scoped |
| Appointment | Time-bound assignment of agent to position |
| AgentRun | Temporary execution attempt, not an employee |
| Task | Durable work with explicit accountable and assigned appointments |
| Delegation | Authorized hand-off, not ownership erasure |
| Report / Review | Distinct immutable submission and assessment |
| Evidence | References or artifacts supporting assertions |
| MemoryRecord | Derived, access-controlled knowledge (not source of truth) |

## Initial non-goals

No default assumption of always-on LLM processes; no full enterprise ERP; no direct human impersonation; no unrestricted cross-agent transcript sharing; no platform rewrite when adding another industry; no claim of factual verification based solely on model output.

## Implementation evidence

The repository contains a **draft contract PR (#1)** and stacked implementation PRs **#2** (SQLite foundation) and **#3** (deterministic delegation/reporting). As documented in those PRs, this is **fixture-only validation code**, not a working autonomous company. No production-ready auth, governance engine or real LLM execution has been demonstrated.

## Related documents

- [M1: Organization & Workforce](01-m1-organization-workforce.md)
- [M2: Work, Memory & Governance](02-m2-work-memory-governance.md)
- [M3: Company Composition & Staffing](03-m3-company-composition.md)
- [M4: Consistency, Contracts & Implementation Gates](04-m4-consistency-contracts.md)
- [M4.3 candidate freeze](m4.3-contract-freeze.md)
- [Project status](../PROJECT_STATUS.md)
