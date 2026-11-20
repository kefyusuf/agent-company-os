"""M5.4-D1/D2/D3: external operation attempt ledger and the scripted adapter seam.

NOT a real provider integration. The only adapter implemented is a
deterministic test fake whose outcomes come from a host-side script. Dispatch
runs inside the caller's SQLite transaction, so the attempt row, the outcome
and the audit events compose one atomic commit (INV-019). A real transport
must replace this with commit-then-dispatch plus crash recovery; from that
moment `unknown` becomes the crash-window reality that reconciliation (D-PR2)
exists for.

FIX-10 no-blind-redispatch is enforced from day one: a new attempt whose
stable guard key (company + target action + resource + target payload,
excluding the attempt id) matches an `unknown`/`reconciling`/`unresolved`
attempt is denied before authorization is re-run. D-PR3 opens exactly one
release valve: an `unresolved` attempt whose recorded human decision is
`reissue` AND already consumed by a created attempt no longer blocks.
`abandon` decisions are never consumed, so an abandoned ambiguity blocks its
guard key forever.

D-PR3 human decisions live in `operation_reconciliations` (owner decision:
dedicated table — approval payload binding does not map to "decide what an
ambiguous external effect means"). Recording a decision and executing a
reissue are separate trusted-host commands: record first, then act, both
fail closed. Reissue always creates a NEW attempt with fresh authorization;
the old attempt stays `unresolved` forever (append-only lineage).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .store import Actor, DomainError, Store, canonical


OPERATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS operation_attempts (
    company_id TEXT NOT NULL REFERENCES companies(id),
    id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN
      ('planned','authorized','dispatched','succeeded','failed','unknown','reconciling','unresolved')),
    attempted_by_appointment_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    rule_digest TEXT NOT NULL,
    consumed_approval_id TEXT,
    reserved_cost_units INTEGER NOT NULL DEFAULT 0,
    guard_key TEXT NOT NULL,
    adapter_ref TEXT,
    evidence_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(company_id,id),
    FOREIGN KEY(company_id,attempted_by_appointment_id)
        REFERENCES appointments(company_id,id)
);
-- Attempt identity/binding columns never change after insertion; only the
-- lifecycle columns (state, adapter_ref, evidence_ref, updated_at) move.
CREATE TRIGGER IF NOT EXISTS immutable_attempt_binding
BEFORE UPDATE ON operation_attempts
BEGIN
    SELECT RAISE(ABORT,'IMMUTABLE_ATTEMPT_BINDING')
    WHERE NEW.action IS NOT OLD.action
       OR NEW.resource_id IS NOT OLD.resource_id
       OR NEW.payload_sha256 IS NOT OLD.payload_sha256
       OR NEW.policy_version IS NOT OLD.policy_version
       OR NEW.rule_digest IS NOT OLD.rule_digest
       OR NEW.attempted_by_appointment_id IS NOT OLD.attempted_by_appointment_id
       OR NEW.consumed_approval_id IS NOT OLD.consumed_approval_id
       OR NEW.reserved_cost_units IS NOT OLD.reserved_cost_units
       OR NEW.guard_key IS NOT OLD.guard_key;
END;
-- D-PR3: dedicated human reconciliation decisions (owner-approved design).
-- Provenance is append-only; exactly one consumption may bind a `reissue`
-- decision to the new attempt it authorizes.
CREATE TABLE IF NOT EXISTS operation_reconciliations (
    company_id TEXT NOT NULL REFERENCES companies(id),
    id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('reissue','abandon')),
    decided_by_appointment_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_by_attempt_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(company_id,id),
    FOREIGN KEY(company_id,attempt_id)
        REFERENCES operation_attempts(company_id,id),
    FOREIGN KEY(company_id,decided_by_appointment_id)
        REFERENCES appointments(company_id,id)
);
CREATE TRIGGER IF NOT EXISTS immutable_reconciliation_decision
BEFORE UPDATE ON operation_reconciliations
BEGIN
    SELECT RAISE(ABORT,'IMMUTABLE_RECONCILIATION_DECISION')
    WHERE NEW.attempt_id IS NOT OLD.attempt_id
       OR NEW.decision IS NOT OLD.decision
       OR NEW.decided_by_appointment_id IS NOT OLD.decided_by_appointment_id
       OR NEW.policy_version IS NOT OLD.policy_version
       OR NEW.expires_at IS NOT OLD.expires_at
       OR NEW.created_at IS NOT OLD.created_at
       OR (OLD.consumed_by_attempt_id IS NOT NULL
           AND NEW.consumed_by_attempt_id IS NOT OLD.consumed_by_attempt_id);
END;
CREATE TRIGGER IF NOT EXISTS deny_reconciliation_decision_delete
BEFORE DELETE ON operation_reconciliations
BEGIN
    SELECT RAISE(ABORT,'IMMUTABLE_RECONCILIATION_DECISION');
END;
"""

ATTEMPT_TRANSITIONS = frozenset({
    ('planned', 'authorized'),
    ('authorized', 'dispatched'),
    ('dispatched', 'succeeded'),
    ('dispatched', 'failed'),
    ('dispatched', 'unknown'),
    ('unknown', 'reconciling'),
    ('reconciling', 'succeeded'),
    ('reconciling', 'failed'),
    ('reconciling', 'unresolved'),
})

OUTCOMES = frozenset({'succeeded', 'failed', 'unknown'})
DECISIONS = frozenset({'reissue', 'abandon'})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guard_key(company: str, target_action: str, target_resource: str,
               target_payload: dict) -> str:
    """FIX-10 stable redispatch guard: attempt identity excluded by design."""
    return hashlib.sha256(canonical({
        'company_id': company,
        'target_action': target_action,
        'target_resource': target_resource,
        'target_payload': target_payload,
    }).encode('utf-8')).hexdigest()


class ScriptedExternalAdapter:
    """Deterministic test adapter — the ONLY adapter implemented in M5.4.

    Outcomes come from a host-side script per target action; dispatch()
    consumes the next scripted outcome. No network, no credentials, no real
    effects. status() reports recorded outcomes for future reconciliation.
    """

    def __init__(self):
        self._script: dict[str, list[dict]] = {}
        self._status_override: dict[str, str] = {}
        self._history: list[dict] = []

    def script(self, action: str, outcome: str) -> None:
        if outcome not in OUTCOMES:
            raise ValueError('invalid scripted outcome')
        self._script.setdefault(action, []).append({'outcome': outcome})

    def script_status(self, adapter_ref: str, outcome: str) -> None:
        """Override the recorded status of one dispatched attempt.

        Models the external world changing after dispatch: a reconciliation
        query may observe a settled outcome the original dispatch could not.
        """
        if outcome not in OUTCOMES:
            raise ValueError('invalid scripted outcome')
        self._status_override[adapter_ref] = outcome

    def dispatch(self, attempt: dict) -> dict:
        queue = self._script.get(attempt['action'])
        if not queue:
            # A test-setup error, not an external outcome: propagates and
            # rolls the whole transaction back (fail closed, no trace).
            raise RuntimeError('no scripted outcome for ' + attempt['action'])
        item = queue.pop(0)
        adapter_ref = 'ADPT-%04d' % (len(self._history) + 1)
        self._history.append({'adapter_ref': adapter_ref,
                              'action': attempt['action'],
                              'outcome': item['outcome'],
                              'attempt_id': attempt.get('attempt_id')})
        return {'outcome': item['outcome'], 'adapter_ref': adapter_ref,
                'evidence_ref': 'evidence:' + adapter_ref}

    def status(self, adapter_ref: str) -> dict | None:
        for item in self._history:
            if item['adapter_ref'] == adapter_ref:
                outcome = self._status_override.get(adapter_ref, item['outcome'])
                return {'outcome': outcome, 'adapter_ref': adapter_ref}
        return None


class ExternalOperationManager:
    """Attempt lifecycle bookkeeping for external actions.

    The gateway re-enforces policy for `operation.dispatch` BEFORE invoking
    this manager; this class then records the attempt lifecycle (planned →
    authorized → dispatched → outcome) inside the caller's transaction.
    Budget semantics follow the owner-approved decisions: reserve at
    authorization, refund on `failed`, hold on `unknown` until reconciliation
    settles (D-PR2).
    """

    def __init__(self, store: Store, adapter: ScriptedExternalAdapter):
        if store.db.in_transaction:
            raise DomainError('OPERATIONS_INITIALIZE_OUTSIDE_TRANSACTION')
        self.store = store
        self.adapter = adapter
        store.db.executescript(OPERATIONS_SCHEMA)

    def _transition(self, company: str, attempt_id: str, old: str, new: str,
                    actor_ref: str, metadata: dict) -> None:
        row = self.store.db.execute(
            'SELECT state FROM operation_attempts WHERE company_id=? AND id=?',
            (company, attempt_id)).fetchone()
        if row is None or row['state'] != old:
            raise DomainError('OPERATION_ATTEMPT_STALE')
        if (old, new) not in ATTEMPT_TRANSITIONS:
            raise DomainError('OPERATION_TRANSITION_INVALID')
        cursor = self.store.db.execute(
            '''UPDATE operation_attempts SET state=?, updated_at=?
               WHERE company_id=? AND id=? AND state=?''',
            (new, _now(), company, attempt_id, old))
        if cursor.rowcount != 1:
            raise DomainError('OPERATION_ATTEMPT_STALE')
        self.store.audit(company, 'operation_attempt', attempt_id,
                         'operation.' + new, actor_ref, {'from': old, **metadata})

    def dispatch(self, actor: Actor, payload: dict, policy_info: dict,
                 receipt_key: str) -> dict:
        """Create and dispatch one attempt inside the caller's transaction.

        payload is the gateway-validated `operation.dispatch` payload:
        {attempt_id, target_action, target_resource, target_payload}.
        policy_info is the resolver result for the operation.dispatch action.
        """
        if not self.store.db.in_transaction:
            raise DomainError('POLICY_REQUIRES_TRANSACTION')
        company = actor.company_id
        attempt_id = payload['attempt_id']
        intent = policy_info['intent']
        guard_key = _guard_key(company, payload['target_action'],
                               payload['target_resource'], payload['target_payload'])
        if self.store.db.execute(
                'SELECT 1 FROM operation_attempts WHERE company_id=? AND id=?',
                (company, attempt_id)).fetchone() is not None:
            raise DomainError('CONFLICT_ATTEMPT_EXISTS')
        # FIX-10: an unresolved ambiguity blocks its guard key until a recorded
        # human `reissue` decision has been consumed by creating a new attempt.
        # `abandon` is never consumable, so it blocks forever (D-PR3).
        prior = self.store.db.execute(
            '''SELECT a.id FROM operation_attempts a
               WHERE a.company_id=? AND a.guard_key=?
                 AND a.state IN ('unknown','reconciling','unresolved')
                 AND NOT EXISTS (
                     SELECT 1 FROM operation_reconciliations d
                     WHERE d.company_id=a.company_id AND d.attempt_id=a.id
                       AND d.decision='reissue'
                       AND d.consumed_by_attempt_id IS NOT NULL)
               LIMIT 1''',
            (company, guard_key)).fetchone()
        if prior is not None:
            raise DomainError('OPERATION_UNRESOLVED_ATTEMPT_EXISTS')
        now = _now()
        self.store.db.execute('''INSERT INTO operation_attempts
            (company_id,id,action,resource_id,payload_sha256,state,
             attempted_by_appointment_id,policy_version,rule_digest,
             consumed_approval_id,reserved_cost_units,guard_key,
             adapter_ref,evidence_ref,created_at,updated_at)
            VALUES (?,?,?,?,?,'planned',?,?,?,?,?,?,?,?,?,?)''',
            (company, attempt_id, 'operation.dispatch', payload['target_resource'],
             intent, actor.appointment_id, policy_info['policy_version'],
             policy_info['rule_digest'], policy_info.get('approval_id'),
             policy_info['reserved_cost_units'], guard_key, None, None, now, now))
        actor_ref = f'agent:{actor.agent_id}@{actor.appointment_id}'
        self._transition(company, attempt_id, 'planned', 'authorized', actor_ref,
                         {'resource_id': payload['target_resource'],
                          'policy_version': policy_info['policy_version']})
        # One adapter call. For the scripted fake this is in-process; a real
        # transport replaces commit-then-dispatch semantics later (D-PR3+).
        call = self.adapter.dispatch({
            'action': payload['target_action'],
            'attempt_id': attempt_id,
            'resource_id': payload['target_resource'],
            'payload_digest': intent,
        })
        outcome = call['outcome']
        self.store.db.execute(
            '''UPDATE operation_attempts SET adapter_ref=?, evidence_ref=?, updated_at=?
               WHERE company_id=? AND id=?''',
            (call['adapter_ref'], call['evidence_ref'], _now(), company, attempt_id))
        self._transition(company, attempt_id, 'authorized', 'dispatched', actor_ref,
                         {'adapter_ref': call['adapter_ref']})
        self._transition(company, attempt_id, 'dispatched', outcome, actor_ref,
                         {'evidence_ref': call['evidence_ref']})
        if outcome == 'failed' and policy_info['reserved_cost_units'] > 0:
            self.store.db.execute(
                'UPDATE policy_budgets SET remaining_units=remaining_units+? WHERE company_id=?',
                (policy_info['reserved_cost_units'], company))
            self.store.audit(company, 'policy_budget', company,
                             'operation.cost_refunded', actor_ref,
                             {'attempt_id': attempt_id,
                              'units': policy_info['reserved_cost_units']})
        # succeeded: cost consumed; unknown: held until reconciliation (D-PR2).
        return {'attempt_id': attempt_id, 'state': outcome,
                'adapter_ref': call['adapter_ref'],
                'resource_id': payload['target_resource']}

    def reconcile(self, actor: Actor, payload: dict, receipt_key: str) -> dict:
        """Reconcile one dispatched attempt inside the caller's transaction.

        The gateway re-enforces the role-gated `operation.reconcile` policy
        before this runs (owner decision: trusted-host command, audited).
        The adapter status for the attempt's adapter_ref is the only evidence:
        settled evidence resolves the attempt (refund on `failed` per the
        owner decisions); unknown or absent evidence records the **terminal**
        `unresolved` state and stops — no state may be inferred (FIX-10).
        """
        if not self.store.db.in_transaction:
            raise DomainError('POLICY_REQUIRES_TRANSACTION')
        company = actor.company_id
        attempt_id = payload['attempt_id']
        attempt = self.store.db.execute(
            '''SELECT state,adapter_ref,reserved_cost_units
               FROM operation_attempts WHERE company_id=? AND id=?''',
            (company, attempt_id)).fetchone()
        if attempt is None:
            raise DomainError('REFERENCE_ATTEMPT_NOT_FOUND')
        if attempt['state'] != 'unknown':
            # Reconciliation starts only from `unknown`: dispatched outcomes
            # are already settled, and `unresolved` is terminal until a human
            # reconciliation decision exists (D-PR3).
            raise DomainError('OPERATION_RECONCILE_NOT_ALLOWED')
        actor_ref = f'agent:{actor.agent_id}@{actor.appointment_id}'
        self._transition(company, attempt_id, 'unknown', 'reconciling', actor_ref,
                         {'receipt': receipt_key})
        evidence = None
        if attempt['adapter_ref'] is not None:
            evidence = self.adapter.status(attempt['adapter_ref'])
        if evidence is None or evidence['outcome'] == 'unknown':
            self._transition(company, attempt_id, 'reconciling', 'unresolved',
                             actor_ref, {'reason': 'no_settled_evidence'})
            return {'attempt_id': attempt_id, 'state': 'unresolved'}
        outcome = evidence['outcome']
        self._transition(company, attempt_id, 'reconciling', outcome, actor_ref,
                         {'adapter_ref': evidence['adapter_ref']})
        if outcome == 'failed' and attempt['reserved_cost_units'] > 0:
            self.store.db.execute(
                'UPDATE policy_budgets SET remaining_units=remaining_units+? WHERE company_id=?',
                (attempt['reserved_cost_units'], company))
            self.store.audit(company, 'policy_budget', company,
                             'operation.cost_refunded', actor_ref,
                             {'attempt_id': attempt_id,
                              'units': attempt['reserved_cost_units']})
        # succeeded: held cost is now consumed; unresolved: still held (D-PR3 settles).
        return {'attempt_id': attempt_id, 'state': outcome}

    def decide(self, actor: Actor, payload: dict, policy_info: dict,
               receipt_key: str) -> dict:
        """Record the human reconciliation decision for one `unresolved` attempt.

        The gateway re-enforces the role-gated `operation.decide` policy before
        this runs (owner decision #2: trusted-host command, audited). This
        method only records provenance — it never dispatches or mutates the
        attempt. Decisions are one-shot provenance rows: `abandon` is final and
        keeps the FIX-10 guard blocked forever; a pending (unconsumed)
        `reissue` decision excludes further decisions until it is consumed.
        """
        if not self.store.db.in_transaction:
            raise DomainError('POLICY_REQUIRES_TRANSACTION')
        company = actor.company_id
        attempt_id = payload['attempt_id']
        decision = payload['decision']
        if decision not in DECISIONS:
            raise DomainError('OPERATION_DECISION_INVALID')
        attempt = self.store.db.execute(
            '''SELECT state FROM operation_attempts WHERE company_id=? AND id=?''',
            (company, attempt_id)).fetchone()
        if attempt is None:
            raise DomainError('REFERENCE_ATTEMPT_NOT_FOUND')
        if attempt['state'] != 'unresolved':
            # Decisions exist only for the terminal ambiguous state; anything
            # else already has settled evidence or is still reconciling.
            raise DomainError('OPERATION_DECIDE_NOT_ALLOWED')
        expires_at = self._expiry(payload['expires_at'])
        if expires_at <= datetime.now(timezone.utc):
            # A decision that can never authorize a reissue is not provenance.
            raise DomainError('OPERATION_DECISION_EXPIRED')
        if self.store.db.execute(
                '''SELECT 1 FROM operation_reconciliations
                   WHERE company_id=? AND attempt_id=? AND decision='abandon' ''',
                (company, attempt_id)).fetchone() is not None:
            raise DomainError('OPERATION_ATTEMPT_ABANDONED')
        if self.store.db.execute(
                '''SELECT 1 FROM operation_reconciliations
                   WHERE company_id=? AND attempt_id=? AND decision='reissue'
                     AND consumed_by_attempt_id IS NULL''',
                (company, attempt_id)).fetchone() is not None:
            raise DomainError('OPERATION_DECISION_ALREADY_PENDING')
        decision_id = payload['decision_id']
        if self.store.db.execute(
                'SELECT 1 FROM operation_reconciliations WHERE company_id=? AND id=?',
                (company, decision_id)).fetchone() is not None:
            raise DomainError('CONFLICT_DECISION_EXISTS')
        self.store.db.execute('''INSERT INTO operation_reconciliations
            (company_id,id,attempt_id,decision,decided_by_appointment_id,
             policy_version,expires_at,consumed_by_attempt_id,created_at)
            VALUES (?,?,?,?,?,?,?,NULL,?)''',
            (company, decision_id, attempt_id, decision, actor.appointment_id,
             policy_info['policy_version'], expires_at.isoformat(), _now()))
        actor_ref = f'agent:{actor.agent_id}@{actor.appointment_id}'
        self.store.audit(company, 'operation_attempt', attempt_id,
                         'operation.decision_recorded', actor_ref,
                         {'decision_id': decision_id, 'decision': decision,
                          'policy_version': policy_info['policy_version'],
                          'receipt': receipt_key})
        return {'decision_id': decision_id, 'attempt_id': attempt_id,
                'decision': decision}

    def reissue(self, actor: Actor, payload: dict, policy_info: dict,
                receipt_key: str) -> dict:
        """Consume a pending `reissue` decision and dispatch a NEW attempt.

        The gateway re-enforces the role-gated `operation.reissue` policy
        (grant scoped to the exact external resource) before this runs — that
        enforcement IS the fresh `planned → authorized` evaluation for the new
        attempt, run at reissue time inside the same transaction. The decision
        is consumed atomically with the new attempt: a downstream failure
        rolls the consumption back, leaving the decision pending (fail closed).
        The old attempt stays `unresolved` forever; the new attempt carries a
        new id and the same stable guard key by construction.
        """
        if not self.store.db.in_transaction:
            raise DomainError('POLICY_REQUIRES_TRANSACTION')
        company = actor.company_id
        attempt_id = payload['attempt_id']
        decision_id = payload['decision_id']
        new_attempt_id = payload['new_attempt_id']
        if new_attempt_id == attempt_id:
            raise DomainError('CONFLICT_ATTEMPT_EXISTS')
        attempt = self.store.db.execute(
            '''SELECT state,guard_key FROM operation_attempts
               WHERE company_id=? AND id=?''',
            (company, attempt_id)).fetchone()
        if attempt is None:
            raise DomainError('REFERENCE_ATTEMPT_NOT_FOUND')
        if attempt['state'] != 'unresolved':
            raise DomainError('OPERATION_REISSUE_NOT_ALLOWED')
        new_key = _guard_key(company, payload['target_action'],
                             payload['target_resource'], payload['target_payload'])
        if new_key != attempt['guard_key']:
            # A reissue redoes the SAME ambiguous effect under fresh
            # authorization; a different payload is simply a new attempt, not
            # a redispatch of this one, and must not consume this decision.
            raise DomainError('OPERATION_REISSUE_GUARD_MISMATCH')
        decision = self.store.db.execute(
            '''SELECT attempt_id,decision,expires_at,consumed_by_attempt_id
               FROM operation_reconciliations WHERE company_id=? AND id=?''',
            (company, decision_id)).fetchone()
        if (decision is None or decision['attempt_id'] != attempt_id
                or decision['decision'] != 'reissue'
                or decision['consumed_by_attempt_id'] is not None):
            raise DomainError('OPERATION_DECISION_NOT_PENDING')
        if self._expiry(decision['expires_at']) <= datetime.now(timezone.utc):
            raise DomainError('OPERATION_DECISION_EXPIRED')
        cursor = self.store.db.execute(
            '''UPDATE operation_reconciliations SET consumed_by_attempt_id=?
               WHERE company_id=? AND id=? AND decision='reissue'
                 AND consumed_by_attempt_id IS NULL''',
            (new_attempt_id, company, decision_id))
        if cursor.rowcount != 1:
            raise DomainError('OPERATION_DECISION_NOT_PENDING')
        actor_ref = f'agent:{actor.agent_id}@{actor.appointment_id}'
        self.store.audit(company, 'operation_attempt', attempt_id,
                         'operation.reissue_decision_consumed', actor_ref,
                         {'decision_id': decision_id,
                          'new_attempt_id': new_attempt_id,
                          'receipt': receipt_key})
        return self.dispatch(actor, {
            'attempt_id': new_attempt_id,
            'target_action': payload['target_action'],
            'target_resource': payload['target_resource'],
            'target_payload': payload['target_payload'],
        }, policy_info, receipt_key)

    @staticmethod
    def _expiry(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise DomainError('OPERATION_DECISION_INVALID_EXPIRY') from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise DomainError('OPERATION_DECISION_INVALID_EXPIRY')
        return parsed
