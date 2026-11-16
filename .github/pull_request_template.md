## What changed

<!-- One paragraph. Reference the milestone/decision that authorizes this change. -->

## Evidence

<!-- Exact observed results — the honest-evidence ledger discipline. -->
- [ ] `python -m unittest discover -s tests -q` — result: <!-- N/N OK -->
- [ ] `python -m compileall -q company_os tests` — clean
- [ ] `scripts/verify.py` — PASS; inventory floor unchanged/raised: <!-- N -->
- [ ] Coverage on `company_os`: <!-- N% total; no security-critical module below threshold -->

## Security review checklist

- [ ] New command/action paths **fail closed** on missing grants, policy, or ambiguous state
- [ ] All SQL parameterized (`?` bindings); no caller-controlled value reaches an SQL statement
- [ ] No secrets, raw payloads, or credentials in audit payloads or logs
- [ ] `Actor` / policy context remains trusted-caller-only; nothing deserialized from model/user input
- [ ] New deny codes listed here: <!-- none / list -->
- [ ] Known gaps stated honestly here: <!-- none / list -->

## Review

- [ ] Self-review completed against the checklist above
- [ ] Independent review: <!-- reviewer name, or "owner self-review until a second reviewer is available (documented norm gap)" -->
