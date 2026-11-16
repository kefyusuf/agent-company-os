"""M5.3-B2: host-only SQLite policy snapshot resolver for fixture commands.

This is NOT an authentication service. All writes to the policy tables in this
prototype are trusted fixture/provisioning operations. A caller with direct
access to Store.db, the FixtureSessionHost issuer, or this resolver bypasses
this security boundary. Do not expose the objects to untrusted callers.

The resolver rejects every action lacking an active company policy, matching
single-appointment scope grant, and currently valid canonical task context.
Approval-gated and positive-cost operations are deliberately NOT executable:
we do not yet have atomic approval consumption or budget reservation.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .policy import (ApprovalSnapshot, Decision, PolicyBook, PolicyContext,
                     PolicyEvaluator, PolicyRule, ScopeGrant)
from .store import Actor, DomainError, Store, canonical


POLICY_SCHEMA = """
CREATE TABLE IF NOT EXISTS company_policy_versions (
    company_id TEXT NOT NULL REFERENCES companies(id),
    version TEXT NOT NULL CHECK(length(version)>0),
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    PRIMARY KEY (company_id,version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_company_policy
    ON company_policy_versions(company_id) WHERE active=1;
CREATE TABLE IF NOT EXISTS company_policy_rules (
    company_id TEXT NOT NULL, version TEXT NOT NULL,
    action TEXT NOT NULL, rule_json TEXT NOT NULL,
    fixed_cost_units INTEGER NOT NULL DEFAULT 0 CHECK(fixed_cost_units>=0),
    PRIMARY KEY(company_id,version,action),
    FOREIGN KEY(company_id,version)
        REFERENCES company_policy_versions(company_id,version)
);
CREATE TABLE IF NOT EXISTS appointment_capabilities (
    company_id TEXT NOT NULL, appointment_id TEXT NOT NULL,
    capability TEXT NOT NULL CHECK(length(capability)>0),
    PRIMARY KEY(company_id,appointment_id,capability),
    FOREIGN KEY(company_id,appointment_id)
        REFERENCES appointments(company_id,id)
);
CREATE TABLE IF NOT EXISTS policy_grants (
    company_id TEXT NOT NULL, id TEXT NOT NULL,
    appointment_id TEXT NOT NULL,
    task_id TEXT,
    action TEXT NOT NULL, resource_id TEXT NOT NULL,
    expires_at TEXT,
    max_cost_units INTEGER NOT NULL DEFAULT 0 CHECK(max_cost_units>=0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    PRIMARY KEY(company_id,id),
    FOREIGN KEY(company_id,appointment_id)
        REFERENCES appointments(company_id,id),
    FOREIGN KEY(company_id,task_id)
        REFERENCES tasks(company_id,id)
);
CREATE INDEX IF NOT EXISTS idx_scoped_policy_grants
    ON policy_grants(company_id,appointment_id,task_id,action,resource_id,active);
-- Grants are bound to the policy version active at issuance, not just action name.
CREATE TABLE IF NOT EXISTS policy_grant_versions (
    company_id TEXT NOT NULL, grant_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    PRIMARY KEY(company_id,grant_id),
    FOREIGN KEY(company_id,grant_id) REFERENCES policy_grants(company_id,id),
    FOREIGN KEY(company_id,policy_version) REFERENCES company_policy_versions(company_id,version)
);
CREATE TRIGGER IF NOT EXISTS grant_policy_version_on_insert
AFTER INSERT ON policy_grants
BEGIN
    INSERT INTO policy_grant_versions(company_id,grant_id,policy_version)
    SELECT NEW.company_id,NEW.id,version FROM company_policy_versions
    WHERE company_id=NEW.company_id AND active=1;
END;
-- Issuance provenance may not be rewritten to reactivate an old grant.
CREATE TRIGGER IF NOT EXISTS deny_grant_version_update
BEFORE UPDATE ON policy_grant_versions
BEGIN
    SELECT RAISE(ABORT,'IMMUTABLE_GRANT_VERSION');
END;
CREATE TRIGGER IF NOT EXISTS deny_grant_version_delete
BEFORE DELETE ON policy_grant_versions
BEGIN
    SELECT RAISE(ABORT,'IMMUTABLE_GRANT_VERSION');
END;
CREATE TABLE IF NOT EXISTS policy_budgets (
    company_id TEXT PRIMARY KEY REFERENCES companies(id),
    remaining_units INTEGER NOT NULL CHECK(remaining_units >= 0)
);
CREATE TABLE IF NOT EXISTS policy_gateway_receipts (
    company_id TEXT NOT NULL REFERENCES companies(id),
    idempotency_key TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    appointment_id TEXT NOT NULL,
    action TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    rule_digest TEXT NOT NULL,
    task_id TEXT,
    original_task_state TEXT,
    original_accountability TEXT,
    original_assignment TEXT,
    resource_id TEXT NOT NULL,
    reserved_cost_units INTEGER NOT NULL DEFAULT 0,
    consumed_approval_id TEXT,
    result_json TEXT NOT NULL,
    PRIMARY KEY(company_id,idempotency_key),
    FOREIGN KEY(company_id,appointment_id) REFERENCES appointments(company_id,id),
    FOREIGN KEY(company_id,task_id) REFERENCES tasks(company_id,id),
    FOREIGN KEY(company_id,consumed_approval_id) REFERENCES policy_approvals(company_id,id)
);
-- A successful command result and its authorization provenance are append-only.
CREATE TRIGGER IF NOT EXISTS deny_gateway_receipt_update
BEFORE UPDATE ON policy_gateway_receipts
BEGIN
    SELECT RAISE(ABORT,'IMMUTABLE_GATEWAY_RECEIPT');
END;
CREATE TRIGGER IF NOT EXISTS deny_gateway_receipt_delete
BEFORE DELETE ON policy_gateway_receipts
BEGIN
    SELECT RAISE(ABORT,'IMMUTABLE_GATEWAY_RECEIPT');
END;
CREATE TABLE IF NOT EXISTS policy_approvals (
    company_id TEXT NOT NULL, id TEXT NOT NULL,
    task_id TEXT, action TEXT NOT NULL, resource_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL, target_revision INTEGER NOT NULL,
    policy_version TEXT NOT NULL, expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
      ('pending','approved','denied','revoked','expired','consumed')),
    approver_kind TEXT NOT NULL, approver_id TEXT NOT NULL,
    consumed_by_receipt TEXT,
    PRIMARY KEY(company_id,id),
    FOREIGN KEY(company_id,task_id) REFERENCES tasks(company_id,id)
);
-- A consumed approval is terminal: it can never be reset or re-attributed.
CREATE TRIGGER IF NOT EXISTS immutable_approval_consumption
BEFORE UPDATE ON policy_approvals
BEGIN
    SELECT RAISE(ABORT,'IMMUTABLE_APPROVAL_CONSUMPTION')
    WHERE OLD.status='consumed'
       OR (OLD.consumed_by_receipt IS NOT NULL
           AND NEW.consumed_by_receipt IS NOT OLD.consumed_by_receipt);
END;
CREATE INDEX IF NOT EXISTS idx_policy_approval_intent
    ON policy_approvals(company_id,task_id,action,resource_id,payload_sha256,target_revision,policy_version);
CREATE TABLE IF NOT EXISTS report_contributor_lineage (
    company_id TEXT NOT NULL REFERENCES companies(id),
    report_id TEXT NOT NULL, report_version INTEGER NOT NULL,
    contributor_agent_id TEXT NOT NULL,
    contribution_kind TEXT NOT NULL
      CHECK(contribution_kind IN ('author','delegator','implementer')),
    attested_by_appointment_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(company_id,report_id,report_version,contributor_agent_id),
    FOREIGN KEY(company_id,report_id,report_version)
        REFERENCES reports(company_id,id,version),
    FOREIGN KEY(company_id,contributor_agent_id) REFERENCES agents(company_id,id),
    FOREIGN KEY(company_id,attested_by_appointment_id)
        REFERENCES appointments(company_id,id)
);
-- Contributor lineage is host-attested provenance: append-only, never rewritten.
CREATE TRIGGER IF NOT EXISTS immutable_lineage_update
BEFORE UPDATE ON report_contributor_lineage
BEGIN SELECT RAISE(ABORT,'IMMUTABLE_REPORT_LINEAGE'); END;
CREATE TRIGGER IF NOT EXISTS immutable_lineage_delete
BEFORE DELETE ON report_contributor_lineage
BEGIN SELECT RAISE(ABORT,'IMMUTABLE_REPORT_LINEAGE'); END;
"""

RULE_FIELDS = frozenset({'action','eligible_roles','required_capabilities',
                         'allowed_task_states','allowed_company_states',
                         'task_required','approval_required',
                         'independent_reviewer_required','risk_level'})
COLLECTION_FIELDS = frozenset({'eligible_roles','required_capabilities',
                               'allowed_task_states','allowed_company_states'})
TASK_FIELD = {
    'task.create': None,
    'task.transition': 'task_id',
    'task.start': 'task_id',
    'task.resume': 'task_id',
    'delegation.offer': 'parent_task_id',
    'delegation.accept': 'delegation_lookup',
    'evidence.add': 'task_id',
    'report.submit': 'task_id',
    'report.begin_review': 'report_lookup',
    'report.review': 'report_lookup',
    'task.accept': 'task_id',
    'report.sources': 'report_lookup',
    'operation.dispatch': None,
}


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError('timezone required')
        return dt
    except (TypeError, ValueError) as exc:
        raise DomainError('POLICY_INVALID_SNAPSHOT_TIME') from exc


class SQLitePolicyResolver:
    """Trusted SQLite-state adapter for the pure PolicyEvaluator.

    Construct once, OUTSIDE Store.tx(): executescript commits an open transaction.
    Gateway calls `enforce` within Store.tx() while the write lock is held.
    """

    def __init__(self, store: Store):
        self.store = store
        if store.db.in_transaction:
            raise DomainError('POLICY_INITIALIZE_OUTSIDE_TRANSACTION')
        store.db.executescript(POLICY_SCHEMA)

    def _book(self, company: str) -> tuple[PolicyBook, dict[str, int]]:
        rows=self.store.db.execute('''SELECT version FROM company_policy_versions
             WHERE company_id=? AND active=1''',(company,)).fetchall()
        if len(rows)!=1:
            raise DomainError('POLICY_NO_ACTIVE_VERSION')
        version=rows[0]['version']
        rules={}
        costs={}
        records=self.store.db.execute('''SELECT action,rule_json,fixed_cost_units
             FROM company_policy_rules WHERE company_id=? AND version=?''',
             (company,version)).fetchall()
        for r in records:
            try:
                data=json.loads(r['rule_json'])
                if not isinstance(data,dict) or set(data)-RULE_FIELDS:
                    raise ValueError('unknown rule field')
                for field in ('task_required', 'approval_required', 'independent_reviewer_required'):
                    if field in data and type(data[field]) is not bool:
                        raise ValueError('invalid boolean policy field')
                for field in COLLECTION_FIELDS:
                    if field in data:
                        if type(data[field]) is not list or any(type(x) is not str for x in data[field]):
                            raise ValueError('invalid rule collection')
                        data[field]=frozenset(data[field])
                rule=PolicyRule(**data)
                if rule.action!=r['action']:
                    raise ValueError('action mismatch')
                if rule.task_required and not rule.allowed_task_states:
                    raise ValueError('task-required policy must enumerate allowed states')
            except (TypeError,ValueError,KeyError) as exc:
                raise DomainError('POLICY_INVALID_STORED_RULE') from exc
            rules[r['action']]=rule
            costs[r['action']]=r['fixed_cost_units']
        if not rules:
            raise DomainError('POLICY_EMPTY_RULEBOOK')
        return PolicyBook(version,rules),costs

    def _task_for_action(self, company: str, action: str, payload: dict,
                         *, replay: bool = False):
        field=TASK_FIELD.get(action,'UNSUPPORTED')
        if field=='UNSUPPORTED':
            raise DomainError('POLICY_UNKNOWN_ACTION')
        if field is None:
            return None
        if field=='delegation_lookup':
            item=self.store.db.execute('''SELECT parent_task_id,revision FROM delegations
                WHERE company_id=? AND id=?''',(company,payload['offer_id'])).fetchone()
            if item is None:
                raise DomainError('REFERENCE_DELEGATION_NOT_FOUND')
            if not replay and item['revision'] != payload['expected_revision']:
                raise DomainError('STALE_REVISION')
            task_id=item['parent_task_id']
        elif field=='report_lookup':
            item=self.store.db.execute('''SELECT task_id FROM reports
                WHERE company_id=? AND id=? AND version=?''',
                (company,payload['report_id'],payload['version'])).fetchone()
            if item is None:
                raise DomainError('REFERENCE_REPORT_NOT_FOUND')
            task_id=item['task_id']
        else:
            task_id=payload[field]
        task=self.store.db.execute('''SELECT * FROM tasks WHERE company_id=? AND id=?''',
                                   (company,task_id)).fetchone()
        if task is None:
            raise DomainError('REFERENCE_TASK_NOT_FOUND')
        # Offer acceptance carries a Delegation revision, not a parent Task revision.
        if (not replay and action != 'delegation.accept' and
            'expected_revision' in payload and
            task['revision'] != payload['expected_revision']):
            raise DomainError('STALE_REVISION')
        return task

    def _grant(self, company: str, appointment: str, task_id: str | None,
               action: str, resource_id: str, policy_version: str) -> ScopeGrant | None:
        rows=self.store.db.execute('''SELECT g.* FROM policy_grants g
           JOIN policy_grant_versions gv ON gv.company_id=g.company_id
               AND gv.grant_id=g.id AND gv.policy_version=?
           WHERE g.company_id=? AND g.appointment_id=? AND g.task_id IS ?
             AND g.action=? AND g.resource_id=? AND g.active=1''',
           (policy_version,company,appointment,task_id,action,resource_id)).fetchall()
        if len(rows)>1:
            raise DomainError('POLICY_AMBIGUOUS_GRANT')
        if not rows:
            return None
        row=rows[0]
        return ScopeGrant(company_id=company,appointment_id=appointment,
            task_id=task_id,actions=frozenset({action}),resources=frozenset({resource_id}),
            expires_at=_timestamp(row['expires_at']),max_cost_units=row['max_cost_units'])

    def _approval(self, company: str, task_id: str | None,
                  action: str, resource_id: str, payload_digest: str,
                  target_revision: int, version: str) -> tuple[ApprovalSnapshot, str] | None:
        rows=self.store.db.execute('''SELECT * FROM policy_approvals
          WHERE company_id=? AND task_id IS ? AND action=? AND resource_id=?
            AND payload_sha256=? AND target_revision=? AND policy_version=?''',
            (company,task_id,action,resource_id,payload_digest,target_revision,version)).fetchall()
        if len(rows)>1:
            raise DomainError('POLICY_AMBIGUOUS_APPROVAL')
        if not rows:
            return None
        a=rows[0]
        return (ApprovalSnapshot(company_id=a['company_id'],task_id=a['task_id'],
          action=a['action'],resource_id=a['resource_id'],
          payload_sha256=a['payload_sha256'],target_revision=a['target_revision'],
          policy_version=a['policy_version'],expires_at=_timestamp(a['expires_at']),
          status=a['status'],approver_kind=a['approver_kind'],approver_id=a['approver_id']),
          a['id'])

    def enforce(self, actor: Actor, action: str, payload: dict,
                *, replay_receipt=None) -> dict:
        """Recheck policy before domain mutation; caller holds Store.tx write lock."""
        if not self.store.db.in_transaction:
            raise DomainError('POLICY_REQUIRES_TRANSACTION')
        self.store.require_actor(actor,actor.company_id)
        book,costs=self._book(actor.company_id)
        rule=book.rules.get(action)
        if rule is None:
            raise DomainError('POLICY_UNKNOWN_ACTION')
        rule_row=self.store.db.execute('''SELECT rule_json,fixed_cost_units FROM company_policy_rules
            WHERE company_id=? AND version=? AND action=?''',
            (actor.company_id,book.version,action)).fetchone()
        if rule_row is None:
            raise DomainError('POLICY_UNKNOWN_ACTION')
        rule_digest=hashlib.sha256(canonical({
            'rule_json':rule_row['rule_json'],
            'fixed_cost_units':rule_row['fixed_cost_units']
        }).encode('utf-8')).hexdigest()
        if replay_receipt is not None and (
            book.version != replay_receipt['policy_version'] or
            rule_digest != replay_receipt['rule_digest']
        ):
            raise DomainError('AUTH_REPLAY_POLICY_CHANGED')
        task=self._task_for_action(actor.company_id,action,payload,
                                   replay=replay_receipt is not None)
        task_id=task['id'] if task is not None else None
        if action == 'operation.dispatch':
            # The grant must scope the exact external resource, e.g. external:publish.
            resource_id = payload['target_resource']
        else:
            resource_id='task:'+task_id if task_id else 'company:'+actor.company_id
        if replay_receipt is not None and action == 'delegation.accept':
            # The original offer revision may be stale (the accept succeeded),
            # but the accepted delegation itself and its reporting chain must
            # still be authoritative. A revoked offer is not an access token.
            offer=self.store.db.execute('''SELECT * FROM delegations
                WHERE company_id=? AND id=?''',
                (actor.company_id,payload['offer_id'])).fetchone()
            if (offer is None or offer['state']!='accepted' or
                offer['target_appointment_id']!=actor.appointment_id or
                offer['parent_task_id']!=task_id):
                raise DomainError('AUTH_REPLAY_DELEGATION_STALE')
            child=self.store.db.execute('''SELECT parent_id,accountable_appointment_id
                FROM tasks WHERE company_id=? AND id=?''',
                (actor.company_id,offer['child_task_id'])).fetchone()
            if (child is None or child['parent_id']!=task_id or
                child['accountable_appointment_id']!=actor.appointment_id):
                raise DomainError('AUTH_REPLAY_DELEGATION_STALE')
            relation=self.store.db.execute('''SELECT target_pos.manager_id,
                delegator.position_id,delegator.status
                FROM appointments target
                JOIN positions target_pos ON target_pos.company_id=target.company_id
                    AND target_pos.id=target.position_id
                JOIN appointments delegator ON delegator.company_id=target.company_id
                    AND delegator.id=?
                WHERE target.company_id=? AND target.id=?''',
                (offer['offered_by_appointment_id'],actor.company_id,
                 actor.appointment_id)).fetchone()
            if (relation is None or relation['status']!='active' or
                relation['manager_id']!=relation['position_id']):
                raise DomainError('AUTH_REPLAY_HIERARCHY_CHANGED')
        if replay_receipt is not None and (
            replay_receipt['task_id'] != task_id or
            replay_receipt['resource_id'] != resource_id or
            (task is not None and
             replay_receipt['original_accountability'] != task['accountable_appointment_id']) or
            (task is not None and
             replay_receipt['original_assignment'] != task['assigned_appointment_id']) or
            replay_receipt['agent_id'] != actor.agent_id or
            replay_receipt['appointment_id'] != actor.appointment_id or
            replay_receipt['action'] != action
        ):
            raise DomainError('AUTH_REPLAY_SCOPE_CHANGED')
        role=self.store.db.execute('''SELECT p.role_ref FROM appointments a
           JOIN positions p ON p.company_id=a.company_id AND p.id=a.position_id
           WHERE a.company_id=? AND a.id=?''',
           (actor.company_id,actor.appointment_id)).fetchone()
        if role is None:
            raise DomainError('AUTH_INVALID_APPOINTMENT')
        company=self.store.db.execute('''SELECT state,revision FROM companies WHERE id=?''',
                                       (actor.company_id,)).fetchone()
        if company is None:
            raise DomainError('REFERENCE_COMPANY_NOT_FOUND')
        caps=frozenset(x['capability'] for x in self.store.db.execute(
             '''SELECT capability FROM appointment_capabilities
                WHERE company_id=? AND appointment_id=?''',
             (actor.company_id,actor.appointment_id)))
        budget=self.store.db.execute('''SELECT remaining_units FROM policy_budgets
             WHERE company_id=?''',(actor.company_id,)).fetchone()
        cost=costs[action]
        reserved_cost=0
        if cost!=0 and replay_receipt is None:
            # Reserve atomically inside the command transaction; a downstream
            # failure rolls the reservation back (automatic refund). A cached
            # replay never re-reserves and never re-charges the company pool.
            if budget is None:
                raise DomainError('BUDGET_NO_ROW')
            if budget['remaining_units'] < cost:
                raise DomainError('BUDGET_EXCEEDED')
            cursor=self.store.db.execute('''UPDATE policy_budgets
                SET remaining_units=remaining_units-?
                WHERE company_id=? AND remaining_units>=?''',
                (cost,actor.company_id,cost))
            if cursor.rowcount!=1:
                raise DomainError('BUDGET_EXCEEDED')
            reserved_cost=cost
        intent='sha256:'+hashlib.sha256(canonical({
            'company_id':actor.company_id,'action':action,'task_id':task_id,
            'resource':resource_id,'payload':payload
        }).encode('utf-8')).hexdigest()
        revision=task['revision'] if task is not None else company['revision']
        author_id=None
        delegated=frozenset()
        if rule.independent_reviewer_required:
            if action not in ('report.review','report.begin_review'):
                raise DomainError('POLICY_REVIEW_LINEAGE_UNAVAILABLE')
            row=self.store.db.execute('''SELECT r.author_agent_id FROM reports r
                WHERE r.company_id=? AND r.id=? AND r.version=?''',
                (actor.company_id,payload['report_id'],payload['version'])).fetchone()
            author_id=row['author_agent_id'] if row else None
            if not author_id:
                raise DomainError('POLICY_REVIEW_LINEAGE_UNAVAILABLE')
            if author_id == actor.agent_id:
                raise DomainError('CONFLICT_INDEPENDENT_REVIEW')
            # A different identity alone is insufficient evidence of
            # independence: a controlled subagent or upstream delegation chain
            # can still create the appearance of independent review. The
            # reviewer must be absent from the host-attested contributor
            # lineage recorded from canonical records at submission time.
            lineage=self.store.db.execute('''SELECT contributor_agent_id
                FROM report_contributor_lineage
                WHERE company_id=? AND report_id=? AND report_version=?''',
                (actor.company_id,payload['report_id'],payload['version'])).fetchall()
            delegated=frozenset(item['contributor_agent_id'] for item in lineage)
            if not delegated:
                # Fail closed when no attested lineage exists at all.
                raise DomainError('POLICY_REVIEW_LINEAGE_UNAVAILABLE')
            if actor.agent_id in delegated:
                # Explicit check only refines the error code; the pure
                # evaluator below would deny the same condition anyway.
                raise DomainError('CONFLICT_INDEPENDENT_REVIEW')
        approval_replay=False
        if replay_receipt is not None and rule.approval_required:
            # Replay provenance: the receipt must reference the approval it
            # consumed at the original committed execution; a consumed
            # approval can never be re-attributed (immutable trigger).
            consumed=self.store.db.execute('''SELECT status,consumed_by_receipt
                FROM policy_approvals WHERE company_id=? AND id=?''',
                (actor.company_id,replay_receipt['consumed_approval_id'])).fetchone()
            if (consumed is None or consumed['status']!='consumed' or
                consumed['consumed_by_receipt']!=replay_receipt['idempotency_key']):
                raise DomainError('AUTH_REPLAY_APPROVAL_STALE')
            approval_replay=True
        ctx=PolicyContext(company_id=actor.company_id,
            principal_company_id=actor.company_id,
            appointment_company_id=actor.company_id,
            appointment_id=actor.appointment_id,appointment_active=True,
            role_ref=role['role_ref'],capabilities=caps,company_state=company['state'],
            task_id=task_id,task_company_id=actor.company_id if task_id else None,
            task_state=(replay_receipt['original_task_state']
                        if replay_receipt is not None else
                        (task['state'] if task else None)),
            resource_id=resource_id,resource_company_id=actor.company_id,
            action=action,payload_sha256=intent,target_revision=revision,
            # A replay is a committed-result lookup, not new spending.
            requested_cost_units=(0 if replay_receipt is not None else cost),
            remaining_budget_units=budget['remaining_units'] if budget else 0,
            actor_agent_id=actor.agent_id,reviewed_author_agent_id=author_id,
            delegated_reviewer_agent_ids=delegated,approval_replay=approval_replay)
        grant=self._grant(actor.company_id,actor.appointment_id,task_id,action,resource_id,book.version)
        approval_id=None
        approval=None
        if rule.approval_required and replay_receipt is None:
            found=self._approval(actor.company_id,task_id,action,resource_id,
                                 intent,revision,book.version)
            if found is not None:
                approval,approval_id=found
        outcome=PolicyEvaluator(book).evaluate(ctx,grant,approval)
        if outcome.decision is Decision.DENY:
            raise DomainError(outcome.reason)
        if outcome.decision is Decision.APPROVAL_REQUIRED:
            raise DomainError(outcome.reason)
        # This policy decision is part of the same SQLite transaction as the
        # domain mutation. A downstream guard failure rolls it back; the gateway
        # records the denial in a separate minimal audit event instead.
        self.store.audit(actor.company_id,'authorization',action,
             'authorization.replay_permitted' if replay_receipt is not None
             else 'authorization.permitted',
             f'agent:{actor.agent_id}@{actor.appointment_id}',
             {'policy_version':book.version,'action':action,'resource_id':resource_id,
              'task_id':task_id})
        return {'policy_version':book.version,'rule_digest':rule_digest,'task_id':task_id,
                'original_task_state':(replay_receipt['original_task_state']
                                      if replay_receipt is not None else
                                      (task['state'] if task else None)),
                'resource_id':resource_id,
                'reserved_cost_units':reserved_cost,
                'approval_id':approval_id,
                'intent':intent,
                'original_accountability':(task['accountable_appointment_id']
                                          if task is not None else None),
                'original_assignment':(task['assigned_appointment_id']
                                      if task is not None else None)}

    @staticmethod
    def _gateway_digest(actor: Actor, action: str, payload: dict) -> str:
        return hashlib.sha256(canonical({
            'company_id':actor.company_id,
            'agent_id':actor.agent_id,
            'appointment_id':actor.appointment_id,
            'action':action,'payload':payload
        }).encode('utf-8')).hexdigest()

    def replay(self, actor: Actor, action: str, payload: dict,
               key: str) -> dict | None:
        """Return a previously committed response only after fresh authorization.

        The record provides the historical task state for checking the original
        action's preconditions; current actor, grant, policy and company checks
        are still mandatory. A receipt is never an authorization credential.
        """
        if not self.store.db.in_transaction:
            raise DomainError('POLICY_REQUIRES_TRANSACTION')
        receipt=self.store.db.execute('''SELECT * FROM policy_gateway_receipts
            WHERE company_id=? AND idempotency_key=?''',
            (actor.company_id,key)).fetchone()
        if receipt is None:
            return None
        digest=self._gateway_digest(actor,action,payload)
        if (receipt['request_digest']!=digest or receipt['action']!=action or
            receipt['agent_id']!=actor.agent_id or
            receipt['appointment_id']!=actor.appointment_id):
            raise DomainError('IDEMPOTENCY_CONFLICT')
        self.enforce(actor,action,payload,replay_receipt=receipt)
        return json.loads(receipt['result_json'])

    def save_gateway_receipt(self, actor: Actor, action: str, payload: dict,
                             key: str, policy_info: dict, result: dict) -> None:
        """Commit the successful command receipt in the domain transaction."""
        if not self.store.db.in_transaction:
            raise DomainError('POLICY_REQUIRES_TRANSACTION')
        self.store.db.execute('''INSERT INTO policy_gateway_receipts
            (company_id,idempotency_key,agent_id,appointment_id,action,
             request_digest,policy_version,rule_digest,task_id,original_task_state,
             original_accountability,original_assignment,resource_id,
             reserved_cost_units,consumed_approval_id,result_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (actor.company_id,key,actor.agent_id,actor.appointment_id,action,
             self._gateway_digest(actor,action,payload),
             policy_info['policy_version'],policy_info['rule_digest'],
             policy_info['task_id'],policy_info['original_task_state'],
             policy_info['original_accountability'],
             policy_info['original_assignment'],policy_info['resource_id'],
             policy_info['reserved_cost_units'],policy_info.get('approval_id'),
             canonical(result)))

    def record_report_lineage(self, actor: Actor, report_id: str,
                              version: int) -> None:
        """Attest report contributors from canonical records, never from text.

        Called inside the same transaction as an accepted submission. Rows are
        append-only (immutable triggers) and feed the independent-review guard.
        """
        if not self.store.db.in_transaction:
            raise DomainError('POLICY_REQUIRES_TRANSACTION')
        report=self.store.db.execute('''SELECT task_id,author_agent_id,
            author_appointment_id FROM reports
            WHERE company_id=? AND id=? AND version=?''',
            (actor.company_id,report_id,version)).fetchone()
        if report is None:
            raise DomainError('REFERENCE_REPORT_NOT_FOUND')
        entries={report['author_agent_id']:'author'}
        task_id=report['task_id']
        while task_id is not None:
            link=self.store.db.execute('''SELECT parent_task_id,
                offered_by_appointment_id,target_appointment_id
                FROM delegations WHERE company_id=? AND child_task_id=?''',
                (actor.company_id,task_id)).fetchone()
            if link is None:
                break
            for appointment,kind in (
                    (link['offered_by_appointment_id'],'delegator'),
                    (link['target_appointment_id'],'implementer')):
                row=self.store.db.execute('''SELECT agent_id FROM appointments
                    WHERE company_id=? AND id=?''',
                    (actor.company_id,appointment)).fetchone()
                if row is not None and row['agent_id'] not in entries:
                    entries[row['agent_id']]=kind
            task_id=link['parent_task_id']
        recorded_at=datetime.now(timezone.utc).isoformat()
        for agent_id,kind in entries.items():
            self.store.db.execute('''INSERT INTO report_contributor_lineage
                (company_id,report_id,report_version,contributor_agent_id,
                 contribution_kind,attested_by_appointment_id,recorded_at)
                VALUES (?,?,?,?,?,?,?)''',
                (actor.company_id,report_id,version,agent_id,kind,
                 report['author_appointment_id'],recorded_at))

    def consume_approval(self, actor: Actor, approval_id: str,
                         receipt_key: str, action: str) -> None:
        """Consume an exact-matched human approval for one command attempt.

        Must run inside the same transaction as the command; a downstream
        failure rolls the approval back to 'approved' (owner-approved FIX-07
        interpretation). A consumed approval is terminal (immutable trigger).
        """
        if not self.store.db.in_transaction:
            raise DomainError('POLICY_REQUIRES_TRANSACTION')
        cursor=self.store.db.execute('''UPDATE policy_approvals
            SET status='consumed', consumed_by_receipt=?
            WHERE company_id=? AND id=? AND status='approved'
              AND approver_kind='human' AND approver_id IS NOT NULL''',
            (receipt_key,actor.company_id,approval_id))
        if cursor.rowcount!=1:
            raise DomainError('AUTH_APPROVAL_ALREADY_CONSUMED')
        self.store.audit(actor.company_id,'approval',approval_id,'approval.consumed',
             f'agent:{actor.agent_id}@{actor.appointment_id}',
             {'action':action,'receipt':receipt_key})
