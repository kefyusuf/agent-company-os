# M1 — Organization, Role & Workforce Contracts

**Status:** Design reconstructed from M1.1–M1.3 discussions, **not executable validation**.  
**Normative precedence:** Later M4.1–M4.3 corrections override earlier ambiguity; see [M4 consistency](04-m4-consistency-contracts.md).

## M1.1 — Organization Ontology

Five logical responsibilities, **not necessarily separate services**:

| Domain | Responsibility |
| --- | --- |
| Company Kernel | Tenant, company lifecycle, global governance |
| Organization Blueprint | Organization topology and composition |
| Agent Workforce | Persistent people/agents, positions, appointments |
| Work Management | Tasks, delegations, reviews and reports |
| Institutional Memory | Durable, scoped institutional knowledge |

The organizational reporting graph and operational collaboration graph are separate.

- **Reporting:** `reports_to`, management of subordinates, executive reporting to Owner.
- **Operational edges:** `DELEGATE`, `REQUEST`, `REVIEW`, `ESCALATE` do **not** grant management authority.
- The organizational reporting graph is acyclic, with one CEO root per active company. CEO's relation to the Human Owner is separate from the position graph.
- Managers retain accountability when delegating. CEO plans and coordinates; it is not the default implementer of all tasks.

## M1.2 — Identity, position, competence and permission

| Entity | Decision |
| --- | --- |
| `RoleDefinition` | Reusable versioned role template |
| `Position` | Company-specific job slot and reporting location |
| `AgentIdentity` | Company-scoped durable employee, separate from LLM session |
| `Appointment` | Agent's assignment to a position; identity may have multiple compatible appointments |
| `Capability` | Competence that can be assessed |
| `Tool` | Interface or action mechanism, **not itself** permission |
| `Permission` / `Policy` | Authorization to act on resource, under context and limits |
| `Task` | Assignment and allowable work scope |
| `AgentRun` | Transient execution attempt with durable history |

**Effective authority** is constrained by authenticated actor, active appointment, relevant role/capability, task/delegation scope, resource policy and company policy. A parent cannot grant rights beyond its delegation authority.

A company may use:

- **Dedicated**: one permanent agent for a position.
- **Combined**: one agent fills multiple compatible positions through separate appointments.
- **Human / External**: real person or vendor covers the role/function; do not invent an AI identity.
- **Unassigned**: position exists with no active employee. Mandatory function coverage must be checked before activation.

Combined roles do not satisfy an **independent reviewer** requirement merely by switching appointments.

### Original reference capability catalog (14)

`cap.planning`, `cap.delegation`, `cap.research`, `cap.reporting`, `cap.review`, `cap.financial-analysis`, `cap.requirements-analysis`, `cap.architecture`, `cap.software-development`, `cap.automated-testing`, `cap.security-assessment`, `cap.customer-communication`, `cap.workforce-management`, `cap.memory-retrieval`.

Capabilities are **requirements**, not proof of mastery. A role or prompt mentioning a capability cannot confer tool access.

## M1.3 — Role catalog and industry independence

A reference software-company catalog of approximately 47 illustrative roles was discussed; **the full exhaustive catalog was not committed as a machine-readable artifact**. Do not infer that it is present or tested.

**Shared functions / candidate roles:** CEO, COO, CFO, accountant, finance analyst, sales, marketing, customer relations, HR/people, security, compliance/legal, procurement, operations.

**Software examples:** CTO, CPO, Product Manager, BA, architect, Engineering Manager, team leads, backend/frontend/fullstack engineers, QA, DevOps/SRE, AppSec, technical delivery/project management.

**Game examples:** Game Director, Studio Producer, Technical Director, Game Designer, Level Designer, Gameplay Programmer, Artist.

**Textile examples:** Design Director, Pattern Specialist, Production Planner/Director, Quality Inspector, Warehouse/Logistics and Procurement.

Common business functions are required **according to function coverage rules**, not necessarily via dedicated AI departments. Especially Finance & Accounting requires an accountable real role/provider; autonomous payments are not implied.

## M1 acceptance boundaries

- Reject reporting cycles, cross-company manager assignments, ambiguous multi-manager positions, invalid role references, unauthorized privilege growth and fake independent review.
- Preserve identity and historical reporting dates on reassignment.
- Separate organization ownership from collaboration/service requests.
- Validate staff competence and authorized task scope before execution.

**Unresolved before production:** Exact role catalog data, machine schema for role variants, competence evaluation method, policy authorization engine and complete historic ORG/ROLE test definitions.
