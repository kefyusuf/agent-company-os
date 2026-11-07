"""Deterministic, fixture-only M5.2 work protocol (not a network authorization boundary).

Uses M5.1 Store's SQLite connection, transactions, receipts and audit records.
No LLMs, external side effects, company provisioning or human approvals.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Callable

from .store import Actor, DomainError, Store, canonical


WORK_SCHEMA = """
CREATE TABLE IF NOT EXISTS delegations (
  company_id TEXT NOT NULL REFERENCES companies(id),
  id TEXT NOT NULL, parent_task_id TEXT NOT NULL,
  offered_by_appointment_id TEXT NOT NULL, target_appointment_id TEXT NOT NULL,
  child_task_id TEXT NOT NULL, objective TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'offered' CHECK(state IN ('offered','accepted','declined','revoked')),
  revision INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(company_id,id), UNIQUE(company_id,child_task_id),
  FOREIGN KEY(company_id,parent_task_id) REFERENCES tasks(company_id,id),
  FOREIGN KEY(company_id,offered_by_appointment_id) REFERENCES appointments(company_id,id),
  FOREIGN KEY(company_id,target_appointment_id) REFERENCES appointments(company_id,id)
);
CREATE TABLE IF NOT EXISTS evidence (
  company_id TEXT NOT NULL REFERENCES companies(id), id TEXT NOT NULL,
  task_id TEXT NOT NULL, created_by_appointment_id TEXT NOT NULL,
  source_ref TEXT NOT NULL, content_sha256 TEXT NOT NULL,
  PRIMARY KEY(company_id,id),
  FOREIGN KEY(company_id,task_id) REFERENCES tasks(company_id,id),
  FOREIGN KEY(company_id,created_by_appointment_id) REFERENCES appointments(company_id,id)
);
CREATE TABLE IF NOT EXISTS reports (
  company_id TEXT NOT NULL REFERENCES companies(id),
  id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version > 0),
  task_id TEXT NOT NULL, author_appointment_id TEXT NOT NULL,
  author_agent_id TEXT NOT NULL, content TEXT NOT NULL,
  PRIMARY KEY(company_id,id,version),
  FOREIGN KEY(company_id,task_id) REFERENCES tasks(company_id,id),
  FOREIGN KEY(company_id,author_appointment_id) REFERENCES appointments(company_id,id)
);
CREATE TABLE IF NOT EXISTS report_evidence (
  company_id TEXT NOT NULL, report_id TEXT NOT NULL,
  report_version INTEGER NOT NULL, evidence_id TEXT NOT NULL,
  PRIMARY KEY(company_id,report_id,report_version,evidence_id),
  FOREIGN KEY(company_id,report_id,report_version) REFERENCES reports(company_id,id,version),
  FOREIGN KEY(company_id,evidence_id) REFERENCES evidence(company_id,id)
);
CREATE TABLE IF NOT EXISTS report_sources (
  company_id TEXT NOT NULL, report_id TEXT NOT NULL,
  report_version INTEGER NOT NULL,
  source_report_id TEXT NOT NULL, source_report_version INTEGER NOT NULL,
  PRIMARY KEY(company_id,report_id,report_version,source_report_id,source_report_version),
  FOREIGN KEY(company_id,report_id,report_version) REFERENCES reports(company_id,id,version),
  FOREIGN KEY(company_id,source_report_id,source_report_version) REFERENCES reports(company_id,id,version)
);
CREATE TABLE IF NOT EXISTS reviews (
  company_id TEXT NOT NULL REFERENCES companies(id), id TEXT NOT NULL,
  report_id TEXT NOT NULL, report_version INTEGER NOT NULL,
  reviewer_appointment_id TEXT NOT NULL, reviewer_agent_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('accepted','changes_requested')),
  reason TEXT NOT NULL,
  PRIMARY KEY(company_id,id),
  UNIQUE(company_id,report_id,report_version),
  FOREIGN KEY(company_id,report_id,report_version) REFERENCES reports(company_id,id,version),
  FOREIGN KEY(company_id,reviewer_appointment_id) REFERENCES appointments(company_id,id)
);
CREATE TABLE IF NOT EXISTS review_contexts (
  company_id TEXT NOT NULL REFERENCES companies(id), task_id TEXT NOT NULL,
  report_id TEXT NOT NULL, report_version INTEGER NOT NULL,
  reviewer_appointment_id TEXT NOT NULL,
  PRIMARY KEY(company_id,task_id),
  FOREIGN KEY(company_id,task_id) REFERENCES tasks(company_id,id),
  FOREIGN KEY(company_id,report_id,report_version) REFERENCES reports(company_id,id,version),
  FOREIGN KEY(company_id,reviewer_appointment_id) REFERENCES appointments(company_id,id)
);
CREATE TRIGGER IF NOT EXISTS immutable_report_update BEFORE UPDATE ON reports
BEGIN SELECT RAISE(ABORT,'immutable_report'); END;
CREATE TRIGGER IF NOT EXISTS immutable_report_delete BEFORE DELETE ON reports
BEGIN SELECT RAISE(ABORT,'immutable_report'); END;
CREATE TRIGGER IF NOT EXISTS immutable_review_update BEFORE UPDATE ON reviews
BEGIN SELECT RAISE(ABORT,'immutable_review'); END;
CREATE TRIGGER IF NOT EXISTS immutable_review_delete BEFORE DELETE ON reviews
BEGIN SELECT RAISE(ABORT,'immutable_review'); END;
CREATE TRIGGER IF NOT EXISTS immutable_evidence_update BEFORE UPDATE ON evidence
BEGIN SELECT RAISE(ABORT,'immutable_evidence'); END;
CREATE TRIGGER IF NOT EXISTS immutable_evidence_delete BEFORE DELETE ON evidence
BEGIN SELECT RAISE(ABORT,'immutable_evidence'); END;
CREATE TRIGGER IF NOT EXISTS immutable_report_refs_delete BEFORE DELETE ON report_sources
BEGIN SELECT RAISE(ABORT,'immutable_report_reference'); END;
CREATE TRIGGER IF NOT EXISTS immutable_report_refs_update BEFORE UPDATE ON report_sources
BEGIN SELECT RAISE(ABORT,'immutable_report_reference'); END;
CREATE TRIGGER IF NOT EXISTS immutable_evidence_refs_delete BEFORE DELETE ON report_evidence
BEGIN SELECT RAISE(ABORT,'immutable_report_reference'); END;
CREATE TRIGGER IF NOT EXISTS immutable_evidence_refs_update BEFORE UPDATE ON report_evidence
BEGIN SELECT RAISE(ABORT,'immutable_report_reference'); END;
"""


class Workflows:
    """Scoped deterministic prototype. Caller-supplied Actor requires a trusted boundary."""

    def __init__(self, store: Store, *, max_delegation_depth: int = 4):
        self.s = store
        self.s.db.executescript(WORK_SCHEMA)
        if max_delegation_depth < 1:
            raise ValueError('max_delegation_depth must be positive')
        self.max_depth = max_delegation_depth

    def _row(self, table: str, company: str, item_id: str):
        # Internal-only table identifiers; never interpolate user input into SQL.
        if table not in {'tasks','appointments','delegations','reports','evidence'}:
            raise AssertionError('unknown table')
        return self.s.db.execute(f'SELECT * FROM {table} WHERE company_id=? AND id=?',
                                 (company,item_id)).fetchone()

    def _actor(self, actor: Actor, *, require_active: bool = True) -> None:
        self.s.require_actor(actor, actor.company_id)
        company = self.s.db.execute('SELECT state FROM companies WHERE id=?',
                                    (actor.company_id,)).fetchone()
        if company is None:
            raise DomainError('REFERENCE_COMPANY_NOT_FOUND')
        if require_active and company['state'] != 'active':
            raise DomainError('AUTH_COMPANY_NOT_ACTIVE')

    def _task(self, actor: Actor, task_id: str, *, owner: bool = True):
        task=self._row('tasks',actor.company_id,task_id)
        if task is None:
            raise DomainError('REFERENCE_TASK_NOT_FOUND')
        if owner and task['accountable_appointment_id'] != actor.appointment_id:
            raise DomainError('AUTH_NOT_ACCOUNTABLE')
        return task

    def _command(self, actor: Actor, key: str, command: str, payload: dict,
                 action: Callable[[], dict]) -> dict:
        if not key or not isinstance(key,str):
            raise DomainError('SCHEMA_REQUIRED_FIELD')
        digest=hashlib.sha256(canonical({'actor':actor.__dict__,'payload':payload}).encode()).hexdigest()
        with self.s.tx():
            self._actor(actor)
            cached=self.s.receipt(actor.company_id,key,command,digest)
            if cached is not None:
                return cached
            result=action()
            self.s.save_receipt(actor.company_id,key,command,digest,result)
            return result

    def _require_revision(self, task, expected_revision: int) -> None:
        if task['revision'] != expected_revision:
            raise DomainError('STALE_REVISION')

    def _advance(self, actor: Actor, task, state: str, event: str, metadata: dict | None = None):
        c=actor.company_id
        self.s.db.execute('UPDATE tasks SET state=?,revision=revision+1 WHERE company_id=? AND id=?',
                          (state,c,task['id']))
        self.s.audit(c,'task',task['id'],event,f'{actor.agent_id}@{actor.appointment_id}',metadata or {})
        return {'id':task['id'],'state':state,'revision':task['revision']+1,'company_id':c}

    def start_task(self, actor: Actor, task_id: str, expected_revision: int, key: str) -> dict:
        payload={'task_id':task_id,'expected_revision':expected_revision}
        def act():
            task=self._task(actor,task_id)
            self._require_revision(task,expected_revision)
            if task['state']!='assigned' or task['assigned_appointment_id']!=actor.appointment_id:
                raise DomainError('STATE_NOT_ASSIGNED_TO_ACTOR')
            return self._advance(actor,task,'in_progress','task.started')
        return self._command(actor,key,'StartTask',payload,act)

    def resume_task(self, actor: Actor, task_id: str, expected_revision: int, key: str) -> dict:
        payload={'task_id':task_id,'expected_revision':expected_revision}
        def act():
            task=self._task(actor,task_id)
            self._require_revision(task,expected_revision)
            if task['state']!='changes_requested':
                raise DomainError('STATE_RESUME_NOT_ALLOWED')
            return self._advance(actor,task,'in_progress','task.resumed')
        return self._command(actor,key,'ResumeTask',payload,act)

    def offer_delegation(self, actor: Actor, parent_task_id: str, offer_id: str,
                         child_task_id: str, target_appointment_id: str,
                         objective: str, expected_revision: int, key: str) -> dict:
        payload={'parent':parent_task_id,'offer':offer_id,'child':child_task_id,
                 'target':target_appointment_id,'objective':objective,'revision':expected_revision}
        def act():
            if not all([offer_id,child_task_id,objective.strip()]):
                raise DomainError('SCHEMA_REQUIRED_FIELD')
            parent=self._task(actor,parent_task_id)
            self._require_revision(parent,expected_revision)
            if parent['state'] not in ('assigned','in_progress'):
                raise DomainError('STATE_DELEGATION_NOT_ALLOWED')
            target=self.s.db.execute('''SELECT a.agent_id,p.manager_id
                FROM appointments a JOIN positions p ON a.company_id=p.company_id AND a.position_id=p.id
                WHERE a.company_id=? AND a.id=? AND a.status='active' ''',
                (actor.company_id,target_appointment_id)).fetchone()
            delegator=self._row('appointments',actor.company_id,actor.appointment_id)
            if target is None or target['manager_id'] != delegator['position_id']:
                raise DomainError('AUTH_NOT_DIRECT_REPORT')
            depth=0
            current=parent
            while current['parent_id'] is not None:
                depth+=1
                if depth >= self.max_depth:
                    raise DomainError('POLICY_DELEGATION_DEPTH')
                current=self._row('tasks',actor.company_id,current['parent_id'])
                if current is None:
                    raise DomainError('REFERENCE_PARENT_NOT_FOUND')
            if depth >= self.max_depth:
                raise DomainError('POLICY_DELEGATION_DEPTH')
            if self._row('tasks',actor.company_id,child_task_id) is not None:
                raise DomainError('CONFLICT_CHILD_TASK_EXISTS')
            try:
                self.s.db.execute('''INSERT INTO delegations
                  (company_id,id,parent_task_id,offered_by_appointment_id,target_appointment_id,child_task_id,objective)
                  VALUES(?,?,?,?,?,?,?)''',
                  (actor.company_id,offer_id,parent_task_id,actor.appointment_id,
                   target_appointment_id,child_task_id,objective))
            except sqlite3.IntegrityError as e:
                raise DomainError('SCHEMA_CONSTRAINT') from e
            result={'id':offer_id,'state':'offered','revision':1,'child_task_id':child_task_id}
            self.s.audit(actor.company_id,'delegation',offer_id,'delegation.offered',
                         f'{actor.agent_id}@{actor.appointment_id}',
                         {'parent':parent_task_id,'target_appointment':target_appointment_id})
            return result
        return self._command(actor,key,'OfferDelegation',payload,act)

    def accept_delegation(self, actor: Actor, offer_id: str, expected_revision: int, key: str) -> dict:
        payload={'offer':offer_id,'revision':expected_revision}
        def act():
            offer=self._row('delegations',actor.company_id,offer_id)
            if offer is None:
                raise DomainError('REFERENCE_DELEGATION_NOT_FOUND')
            if offer['revision'] != expected_revision:
                raise DomainError('STALE_REVISION')
            if offer['state']!='offered':
                raise DomainError('STATE_DELEGATION_NOT_OFFERED')
            if actor.appointment_id!=offer['target_appointment_id']:
                raise DomainError('AUTH_NOT_OFFER_TARGET')
            parent=self._row('tasks',actor.company_id,offer['parent_task_id'])
            if parent is None or parent['state'] not in ('assigned','in_progress'):
                raise DomainError('STATE_PARENT_NOT_ACTIVE')
            if parent['accountable_appointment_id']!=offer['offered_by_appointment_id']:
                raise DomainError('AUTH_DELEGATION_STALE')
            parent_owner=self._row('appointments',actor.company_id,offer['offered_by_appointment_id'])
            if parent_owner is None or parent_owner['status']!='active':
                raise DomainError('AUTH_DELEGATOR_REVOKED')
            target_pos=self.s.db.execute('''SELECT p.manager_id FROM positions p
                JOIN appointments a ON a.company_id=p.company_id AND a.position_id=p.id
                WHERE a.company_id=? AND a.id=?''',
                (actor.company_id,actor.appointment_id)).fetchone()
            if target_pos is None or target_pos['manager_id']!=parent_owner['position_id']:
                raise DomainError('AUTH_HIERARCHY_CHANGED')
            try:
                self.s.db.execute('''INSERT INTO tasks
                    (company_id,id,parent_id,accountable_appointment_id,
                    assigned_appointment_id,objective,state)
                    VALUES(?,?,?,?,?,?,'assigned')''',
                    (actor.company_id,offer['child_task_id'],offer['parent_task_id'],
                     actor.appointment_id,actor.appointment_id,offer['objective']))
            except sqlite3.IntegrityError as e:
                raise DomainError('SCHEMA_CONSTRAINT') from e
            self.s.db.execute('UPDATE delegations SET state=?,revision=revision+1 WHERE company_id=? AND id=?',
                              ('accepted',actor.company_id,offer_id))
            self.s.audit(actor.company_id,'task',offer['child_task_id'],'task.created_from_delegation',
                         f'{actor.agent_id}@{actor.appointment_id}',{'parent':offer['parent_task_id'],'delegation':offer_id})
            self.s.audit(actor.company_id,'delegation',offer_id,'delegation.accepted',
                         f'{actor.agent_id}@{actor.appointment_id}',{'child':offer['child_task_id']})
            return {'id':offer_id,'state':'accepted','revision':expected_revision+1,
                    'child_task_id':offer['child_task_id']}
        return self._command(actor,key,'AcceptDelegation',payload,act)

    def add_evidence(self, actor: Actor, task_id: str, evidence_id: str,
                     source_ref: str, content_sha256: str, key: str) -> dict:
        payload={'task':task_id,'id':evidence_id,'source':source_ref,'sha256':content_sha256}
        def act():
            task=self._task(actor,task_id)
            if task['state']!='in_progress':
                raise DomainError('STATE_EVIDENCE_NOT_ALLOWED')
            if not evidence_id or not source_ref.strip() or re.fullmatch(r'[0-9a-f]{64}',content_sha256) is None:
                raise DomainError('SCHEMA_INVALID_EVIDENCE')
            try:
                self.s.db.execute('''INSERT INTO evidence VALUES(?,?,?,?,?,?)''',
                                  (actor.company_id,evidence_id,task_id,actor.appointment_id,
                                   source_ref,content_sha256))
            except sqlite3.IntegrityError as e:
                raise DomainError('SCHEMA_CONSTRAINT') from e
            self.s.audit(actor.company_id,'evidence',evidence_id,'evidence.referenced',
                         f'{actor.agent_id}@{actor.appointment_id}',{'task':task_id,'source':source_ref})
            return {'id':evidence_id,'task_id':task_id}
        return self._command(actor,key,'AddEvidence',payload,act)

    def submit_report(self, actor: Actor, task_id: str, report_id: str, version: int,
                      content: str, expected_revision: int, key: str,
                      *, evidence_ids: tuple[str,...] = (),
                      source_reports: tuple[tuple[str,int],...] = ()) -> dict:
        payload={'task':task_id,'report':report_id,'version':version,'content':content,
                 'revision':expected_revision,'evidence':evidence_ids,'sources':source_reports}
        def act():
            task=self._task(actor,task_id)
            self._require_revision(task,expected_revision)
            if task['state']!='in_progress':
                raise DomainError('STATE_REPORT_NOT_ALLOWED')
            if not report_id or not content.strip() or not isinstance(version,int) or version<1:
                raise DomainError('SCHEMA_INVALID_REPORT')
            if not (evidence_ids or source_reports):
                raise DomainError('STATE_REPORT_REQUIRES_SOURCES')
            if len(set(evidence_ids))!=len(evidence_ids) or len(set(source_reports))!=len(source_reports):
                raise DomainError('SCHEMA_DUPLICATE_SOURCE')
            prev=self.s.db.execute('SELECT MAX(version) AS version FROM reports WHERE company_id=? AND id=?',
                                  (actor.company_id,report_id)).fetchone()['version']
            if version != (prev or 0)+1:
                raise DomainError('STATE_REPORT_VERSION_SEQUENCE')
            if prev:
                old=self.s.db.execute('''SELECT task_id,author_agent_id FROM reports
                  WHERE company_id=? AND id=? AND version=?''',
                  (actor.company_id,report_id,prev)).fetchone()
                if old['task_id']!=task_id or old['author_agent_id']!=actor.agent_id:
                    raise DomainError('AUTH_REPORT_AUTHOR_CHANGED')
            for evidence_id in evidence_ids:
                evidence=self._row('evidence',actor.company_id,evidence_id)
                if evidence is None or evidence['task_id']!=task_id:
                    raise DomainError('REFERENCE_EVIDENCE_NOT_OWNED')
            for ref_id,ref_version in source_reports:
                source=self.s.db.execute('''SELECT task_id FROM reports
                    WHERE company_id=? AND id=? AND version=?''',
                    (actor.company_id,ref_id,ref_version)).fetchone()
                if source is None:
                    raise DomainError('REFERENCE_SOURCE_REPORT_NOT_FOUND')
                child=self._row('tasks',actor.company_id,source['task_id'])
                if child is None or child['parent_id']!=task_id or child['state']!='accepted':
                    raise DomainError('STATE_SOURCE_CHILD_NOT_ACCEPTED')
                approved=self.s.db.execute('''SELECT 1 FROM reviews WHERE
                    company_id=? AND report_id=? AND report_version=? AND decision='accepted' ''',
                    (actor.company_id,ref_id,ref_version)).fetchone()
                if not approved:
                    raise DomainError('STATE_SOURCE_REPORT_NOT_ACCEPTED')
            self.s.db.execute('''INSERT INTO reports VALUES(?,?,?,?,?,?,?)''',
                              (actor.company_id,report_id,version,task_id,
                               actor.appointment_id,actor.agent_id,content))
            for evidence_id in evidence_ids:
                self.s.db.execute('INSERT INTO report_evidence VALUES (?,?,?,?)',
                                  (actor.company_id,report_id,version,evidence_id))
            for ref_id,ref_version in source_reports:
                self.s.db.execute('INSERT INTO report_sources VALUES (?,?,?,?,?)',
                                  (actor.company_id,report_id,version,ref_id,ref_version))
            self._advance(actor,task,'submitted','task.submitted',{'report':report_id,'version':version})
            self.s.audit(actor.company_id,'report',report_id,'report.submitted',
                         f'{actor.agent_id}@{actor.appointment_id}',
                         {'task':task_id,'version':version,'evidence_count':len(evidence_ids),
                          'source_report_count':len(source_reports)})
            return {'id':report_id,'version':version,'task_id':task_id,'state':'submitted'}
        return self._command(actor,key,'SubmitReport',payload,act)

    def begin_review(self, actor: Actor, report_id: str, version: int, key: str) -> dict:
        payload={'report':report_id,'version':version}
        def act():
            report=self.s.db.execute('''SELECT * FROM reports WHERE company_id=? AND id=? AND version=?''',
                (actor.company_id,report_id,version)).fetchone()
            if report is None:
                raise DomainError('REFERENCE_REPORT_NOT_FOUND')
            task=self._task(actor,report['task_id'],owner=False)
            if task['state']!='submitted':
                raise DomainError('STATE_REVIEW_NOT_ALLOWED')
            if task['parent_id'] is None:
                raise DomainError('AUTH_OWNER_REVIEW_NOT_IMPLEMENTED')
            parent=self._row('tasks',actor.company_id,task['parent_id'])
            if parent is None or parent['accountable_appointment_id']!=actor.appointment_id:
                raise DomainError('AUTH_NOT_REPORT_REVIEWER')
            if report['author_agent_id']==actor.agent_id:
                raise DomainError('CONFLICT_SAME_AGENT_IDENTITY')
            self.s.db.execute('''INSERT INTO review_contexts VALUES(?,?,?,?,?)''',
                              (actor.company_id,task['id'],report_id,version,actor.appointment_id))
            return self._advance(actor,task,'under_review','task.review_started',
                                 {'report':report_id,'version':version})
        return self._command(actor,key,'BeginReview',payload,act)

    def review_report(self, actor: Actor, report_id: str, version: int,
                      review_id: str, decision: str, reason: str, key: str) -> dict:
        payload={'report':report_id,'version':version,'review':review_id,'decision':decision,'reason':reason}
        def act():
            if decision not in ('accepted','changes_requested') or not reason.strip() or not review_id:
                raise DomainError('SCHEMA_INVALID_REVIEW')
            report=self.s.db.execute('''SELECT * FROM reports
                WHERE company_id=? AND id=? AND version=?''',
                (actor.company_id,report_id,version)).fetchone()
            if report is None:
                raise DomainError('REFERENCE_REPORT_NOT_FOUND')
            task=self._task(actor,report['task_id'],owner=False)
            if task['state']!='under_review':
                raise DomainError('STATE_REVIEW_NOT_ALLOWED')
            if task['parent_id'] is None:
                raise DomainError('AUTH_OWNER_REVIEW_NOT_IMPLEMENTED')
            parent=self._row('tasks',actor.company_id,task['parent_id'])
            if parent is None or parent['accountable_appointment_id']!=actor.appointment_id:
                raise DomainError('AUTH_NOT_REPORT_REVIEWER')
            if report['author_agent_id']==actor.agent_id:
                raise DomainError('CONFLICT_SAME_AGENT_IDENTITY')
            context=self.s.db.execute('''SELECT * FROM review_contexts WHERE
                company_id=? AND task_id=?''',(actor.company_id,task['id'])).fetchone()
            if (context is None or context['report_id']!=report_id or
                    context['report_version']!=version or
                    context['reviewer_appointment_id']!=actor.appointment_id):
                raise DomainError('AUTH_REVIEW_CONTEXT_MISMATCH')
            try:
                self.s.db.execute('''INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)''',
                                  (actor.company_id,review_id,report_id,version,actor.appointment_id,
                                   actor.agent_id,decision,reason))
            except sqlite3.IntegrityError as e:
                raise DomainError('SCHEMA_CONSTRAINT') from e
            self.s.db.execute('DELETE FROM review_contexts WHERE company_id=? AND task_id=?',
                              (actor.company_id,task['id']))
            new_state='under_review' if decision=='accepted' else 'changes_requested'
            self._advance(actor,task,new_state,'task.reviewed',{'decision':decision,'review_id':review_id})
            self.s.audit(actor.company_id,'review',review_id,'review.'+decision,
                         f'{actor.agent_id}@{actor.appointment_id}',{'report':report_id,'version':version})
            return {'id':review_id,'decision':decision,'task_id':task['id'],'task_state':new_state}
        return self._command(actor,key,'ReviewReport',payload,act)

    def accept_task(self, actor: Actor, task_id: str, expected_revision: int, key: str) -> dict:
        payload={'task':task_id,'revision':expected_revision}
        def act():
            task=self._task(actor,task_id,owner=False)
            self._require_revision(task,expected_revision)
            if task['state']!='under_review' or task['parent_id'] is None:
                raise DomainError('STATE_TASK_NOT_REVIEWABLE')
            parent=self._row('tasks',actor.company_id,task['parent_id'])
            if parent is None or parent['accountable_appointment_id']!=actor.appointment_id:
                raise DomainError('AUTH_NOT_REPORT_REVIEWER')
            pending=self.s.db.execute('''SELECT 1 FROM delegations WHERE company_id=? AND parent_task_id=? AND state='offered' LIMIT 1''',
                (actor.company_id,task_id)).fetchone()
            if pending:
                raise DomainError('STATE_PENDING_DELEGATION')
            blocked=self.s.db.execute('''SELECT 1 FROM tasks
                WHERE company_id=? AND parent_id=? AND state!='accepted' LIMIT 1''',
                (actor.company_id,task_id)).fetchone()
            if blocked:
                raise DomainError('STATE_REQUIRED_CHILD_NOT_ACCEPTED')
            accepted=self.s.db.execute('''SELECT r.id,r.version FROM reports r JOIN reviews v
              ON r.company_id=v.company_id AND r.id=v.report_id AND r.version=v.report_version
              WHERE r.company_id=? AND r.task_id=? AND v.decision='accepted'
              ORDER BY r.version DESC LIMIT 1''',(actor.company_id,task_id)).fetchone()
            if accepted is None:
                raise DomainError('STATE_ACCEPTED_REPORT_REQUIRED')
            children=self.s.db.execute('SELECT id FROM tasks WHERE company_id=? AND parent_id=?',
                (actor.company_id,task_id)).fetchall()
            for child in children:
                linked=self.s.db.execute('''SELECT 1 FROM report_sources rs
                  JOIN reports sr ON sr.company_id=rs.company_id
                    AND sr.id=rs.source_report_id AND sr.version=rs.source_report_version
                  JOIN reviews rv ON rv.company_id=sr.company_id AND rv.report_id=sr.id
                    AND rv.report_version=sr.version AND rv.decision='accepted'
                  WHERE rs.company_id=? AND rs.report_id=? AND rs.report_version=?
                    AND sr.task_id=? LIMIT 1''',
                  (actor.company_id,accepted['id'],accepted['version'],child['id'])).fetchone()
                if not linked:
                    raise DomainError('STATE_CHILD_EVIDENCE_NOT_LINKED')
            return self._advance(actor,task,'accepted','task.accepted')
        return self._command(actor,key,'AcceptTask',payload,act)

    def report_sources(self, actor: Actor, report_id: str, version: int) -> dict:
        """Accessible only to author or its direct parent-task accountable reviewer."""
        self._actor(actor,require_active=False)
        report=self.s.db.execute('''SELECT * FROM reports WHERE company_id=? AND id=? AND version=?''',
                                 (actor.company_id,report_id,version)).fetchone()
        if report is None:
            raise DomainError('REFERENCE_REPORT_NOT_FOUND')
        task=self._task(actor,report['task_id'],owner=False)
        parent=self._row('tasks',actor.company_id,task['parent_id']) if task['parent_id'] else None
        if actor.appointment_id not in (report['author_appointment_id'],
                                        parent['accountable_appointment_id'] if parent else None):
            raise DomainError('AUTH_REPORT_SCOPE')
        evidence=[r['evidence_id'] for r in self.s.db.execute('''SELECT evidence_id FROM report_evidence
              WHERE company_id=? AND report_id=? AND report_version=? ORDER BY evidence_id''',
              (actor.company_id,report_id,version))]
        sources=[(r['source_report_id'],r['source_report_version']) for r in self.s.db.execute(
              '''SELECT source_report_id,source_report_version FROM report_sources
              WHERE company_id=? AND report_id=? AND report_version=?
              ORDER BY source_report_id,source_report_version''',
              (actor.company_id,report_id,version))]
        return {'id':report_id,'version':version,'evidence_ids':evidence,'source_reports':sources}
