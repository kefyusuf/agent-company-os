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
- Test-setup helper INSERTs (`tests/test_policy_gateway.py` setUp/grant helpers, lines 14/24/41/51 and later): flagged as SQL injection; all values are `?`-bound — data-flow misattribution. Confirmed false positive by the owner (2026-11-20, keep-as-is per ask-mode).
- `company_os/transport.py` `urlopen` calls (T-PR2, 2026-11-27): flagged as SSRF. Review found the pre-network allowlist/scheme/credential validation intact but **one genuine vector**: default redirect following could bypass the allowlist. Fixed properly — redirects are denied (`_DenyRedirects`), surfaced as `unknown`, contract-tested with zero follow-up requests. DNS-rebinding/resolved-IP pinning is deferred to T-PR3 as a gate item (the loopback contract harness makes private-IP blocking wrong for this stage). Recorded as a **partially valid finding, resolved by fix**, not a suppression.
- `tests/test_transport.py` `_db_values` table-name interpolation (2026-11-27): flagged as SQL injection; the value was a hardcoded test literal (no caller-controlled input), but the pattern was real. Fixed by rewriting as three explicit parameterized queries per owner decision — recorded as **valid pattern, resolved by fix**.
- **G-T10 independent deep scan (2026-11-28, owner decision Q4/T-PR3):** Mimosa deep scan of the transport slice (`transport.py`, `operations.py`, `gateway.py`), scanId `scan-2026-11-28T04-44-45.355Z-9990585fa33f`, seal `sha256:7029ac2a4b04d73662e65cbd3fc5eb7b2e7c0af0c1dbfd836807e8bd11d91593` — **4 high findings, all occurrences of the registered store.py savepoint false positive above; zero findings in the transport slice**. Recorded as independent transport-review evidence.

## Reporting

Report vulnerabilities privately to the repository owner (kefyusuf). Do not open public issues for untriaged security claims; the milestone ledger tracks every accepted finding with exact evidence.
