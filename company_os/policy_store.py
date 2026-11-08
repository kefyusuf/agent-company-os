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
    resource_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY(company_id,idempotency_key),
    FOREIGN KEY(company_id,appointment_id) REFERENCES appointments(company_id,id),
    FOREIGN KEY(company_id,task_id) REFERENCES tasks(company_id,id)
);
CREATE TABLE IF NOT EXISTS policy_approvals (
    company_id TEXT NOT NULL, id TEXT NOT NULL,
    task_id TEXT, action TEXT NOT NULL, resource_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL, target_revision INTEGER NOT NULL,
    policy_version TEXT NOT NULL, expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
      ('pending','approved','denied','revoked','expired','consumed')),
    approver_kind TEXT NOT NULL, approver_id TEXT NOT NULL,
    PRIMARY KEY(company_id,id),
    FOREIGN KEY(company_id,task_id) REFERENCES tasks(company_id,id)
);
CREATE INDEX IF NOT EXISTS idx_policy_approval_intent
    ON policy_approvals(company_id,task_id,action,resource_id,payload_sha256,target_revision,policy_version);
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
                  target_revision: int, version: str) -> ApprovalSnapshot | None:
        rows=self.store.db.execute('''SELECT * FROM policy_approvals
          WHERE company_id=? AND task_id IS ? AND action=? AND resource_id=?
            AND payload_sha256=? AND target_revision=? AND policy_version=?''',
            (company,task_id,action,resource_id,payload_digest,target_revision,version)).fetchall()
        if len(rows)>1:
            raise DomainError('POLICY_AMBIGUOUS_APPROVAL')
        if not rows:
            return None
        a=rows[0]
        return ApprovalSnapshot(company_id=a['company_id'],task_id=a['task_id'],
          action=a['action'],resource_id=a['resource_id'],
          payload_sha256=a['payload_sha256'],target_revision=a['target_revision'],
          policy_version=a['policy_version'],expires_at=_timestamp(a['expires_at']),
          status=a['status'],approver_kind=a['approver_kind'],approver_id=a['approver_id'])

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
        resource_id='task:'+task_id if task_id else 'company:'+actor.company_id
        if replay_receipt is not None and (
            replay_receipt['task_id'] != task_id or
            replay_receipt['resource_id'] != resource_id or
            (task is not None and
             replay_receipt['original_accountability'] != task['accountable_appointment_id']) or
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
        # Do not allow positive-cost actions until transactionally reserving funds.
        if cost!=0:
            raise DomainError('BUDGET_RESERVATION_NOT_IMPLEMENTED')
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
            # A different identity is insufficient evidence of independence:
            # a controlled subagent or upstream delegation chain can still
            # create the appearance of independent review. Fail closed until
            # trusted contributor lineage is stored and verified.
            raise DomainError('POLICY_REVIEW_LINEAGE_UNAVAILABLE')
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
            requested_cost_units=cost,
            remaining_budget_units=budget['remaining_units'] if budget else 0,
            actor_agent_id=actor.agent_id,reviewed_author_agent_id=author_id,
            delegated_reviewer_agent_ids=delegated)
        grant=self._grant(actor.company_id,actor.appointment_id,task_id,action,resource_id,book.version)
        approval=self._approval(actor.company_id,task_id,action,resource_id,
                                intent,revision,book.version) if rule.approval_required else None
        outcome=PolicyEvaluator(book).evaluate(ctx,grant,approval)
        if outcome.decision is Decision.DENY:
            raise DomainError(outcome.reason)
        if outcome.decision is Decision.APPROVAL_REQUIRED:
            raise DomainError(outcome.reason)
        # No single-use approval consumption yet. Even an approved fixture value
        # MUST NOT enable the actual command here.
        if rule.approval_required:
            raise DomainError('AUTH_APPROVAL_CONSUMPTION_NOT_IMPLEMENTED')
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
                'original_accountability':(task['accountable_appointment_id']
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
             original_accountability,resource_id,result_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (actor.company_id,key,actor.agent_id,actor.appointment_id,action,
             self._gateway_digest(actor,action,payload),
             policy_info['policy_version'],policy_info['rule_digest'],
             policy_info['task_id'],policy_info['original_task_state'],
             policy_info['original_accountability'],policy_info['resource_id'],
             canonical(result)))
