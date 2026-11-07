# ADR 0001 — Local contract-validation stack

**Status:** Accepted for the first vertical slice only  
**Date:** 2026-11-08  
**Contract baseline:** `ai-company-os.core@0.1.0-rc1`

## Decision

Use **Python 3.11+**, the standard-library **`sqlite3`** module, and **`unittest`** to demonstrate the minimum durable contract behavior locally, without running an API server or LLM.

This is a *validation stack*, not a permanent, platform-wide language decision. Business contracts remain framework- and model-provider-independent.

## Reasons

- SQLite supports foreign keys, unique constraints, and transactional state + audit writes in one local file.
- Python standard-library tools make local tests reproducible without network setup, package installs, containers, or paid CI minutes.
- Separate domain commands and their stored records are intended to be replaceable by another runtime/storage implementation later.
- The exercise can validate failure paths before opening real external integrations.

## Non-decisions

No selection of an HTTP framework, cloud architecture, production database, agent framework, LLM vendor, message broker, or vector store.

## Consequences / risks

- This is **not** a secure multi-user production service. `Actor` is caller-asserted and **not authenticated**.
- `Store.bootstrap(...)` is fixture-only and disabled by default; never expose fixture mode as a production API.
- Only the simplest tenant-scope and appointment checks exist; project-level ACLs and M2.3 policy/approval enforcement do not.
- Existing `M4.3` schemas, transition guards, and 40 conformance cases are **not fully implemented**.
- No automatic company activation; fixture stays `inactive`.
- SQLite/standard-library implementation may be revised once requirements and test evidence justify it.
