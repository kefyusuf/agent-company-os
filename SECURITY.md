# Security Policy & Dependency Manifest

## Stage and NO-GO summary

This repository is a **local deterministic fixture prototype** — not a production system. External side effects, real LLM/tool execution, production authentication, network transport and autonomous deployment remain **NO-GO**. See [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md) for the current honest ledger.

## Dependency manifest (SBOM-equivalent)

The runtime (`company_os/`) uses **only the Python standard library** — no third-party runtime dependencies.

| Component | Version | Scope | Notes |
| --- | --- | --- | --- |
| Python | 3.13 | runtime | stdlib only (`sqlite3`, `hashlib`, `hmac`, `secrets`, `unittest`, …) |
| ruff | 0.17.5 (pinned in CI) | CI/lint only | never imported at runtime |
| coverage | 7.17.3 (pinned in CI) | CI/coverage only | never imported at runtime |
| mypy | 2.4.1 (pinned in CI) | CI/type-check only | never imported at runtime; checks `company_os` only |

A tool-generated SBOM (CycloneDX/SPDX) is intentionally deferred: with zero third-party runtime dependencies the manifest above is complete and hand-verifiable. It must be regenerated whenever a runtime dependency is introduced.

## False-positive registry

Static-analysis dispositions are recorded, not silently ignored (per the defect-management norm: reviewed, justified, narrow, auditable):

- `company_os/store.py` savepoint identifiers (`SAVEPOINT command_sp_{n}`): f-string built **only** from a fixed literal prefix and an internal integer counter; no caller-controlled value reaches the statement. Confirmed false positive by the owner — recorded in PR #11 and re-confirmed on every commit report.
- `gateway.execute(...)` calls in tests: the `CommandGateway.execute` **Python method** is name-collided with `sqlite3.Connection.execute` by the scanner. Confirmed false positive repeatedly (PRs #15–#17, #20 bodies); the tool's ask-mode was used with explicit owner confirmation per write.

## Reporting

Report vulnerabilities privately to the repository owner (kefyusuf). Do not open public issues for untriaged security claims; the milestone ledger tracks every accepted finding with exact evidence.
