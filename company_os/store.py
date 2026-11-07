"""SQLite foundation for M4.4. Only the demo fixture may bootstrap state.

The Actor is asserted by a trusted caller: real authentication, full policy
resolution, approvals and live external operations are NOT implemented.
Never serve this class directly to untrusted clients.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


class DomainError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Actor:
    company_id: str
    agent_id: str
    appointment_id: str


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS companies (
  id TEXT PRIMARY KEY NOT NULL,
  state TEXT NOT NULL DEFAULT 'inactive' CHECK(state IN ('inactive','active','paused','suspended','archived')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0)
);
CREATE TABLE IF NOT EXISTS positions (
  company_id TEXT NOT NULL REFERENCES companies(id),
  id TEXT NOT NULL, role_ref TEXT NOT NULL CHECK(length(role_ref) > 0),
  manager_id TEXT, PRIMARY KEY(company_id,id),
  FOREIGN KEY(company_id,manager_id) REFERENCES positions(company_id,id),
  CHECK(manager_id IS NULL OR manager_id <> id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_ceo ON positions(company_id)
  WHERE role_ref='core.ceo@1';
CREATE TABLE IF NOT EXISTS agents (
  company_id TEXT NOT NULL REFERENCES companies(id), id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('permanent','temporary')),
  PRIMARY KEY(company_id,id)
);
CREATE TABLE IF NOT EXISTS appointments (
  company_id TEXT NOT NULL, id TEXT NOT NULL,
  agent_id TEXT NOT NULL, position_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','ended')),
  PRIMARY KEY(company_id,id),
  FOREIGN KEY(company_id,agent_id) REFERENCES agents(company_id,id),
  FOREIGN KEY(company_id,position_id) REFERENCES positions(company_id,id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_appointment ON appointments(company_id,position_id)
  WHERE status='active';
CREATE TABLE IF NOT EXISTS tasks (
  company_id TEXT NOT NULL REFERENCES companies(id), id TEXT NOT NULL,
  parent_id TEXT, accountable_appointment_id TEXT NOT NULL,
  assigned_appointment_id TEXT, objective TEXT NOT NULL CHECK(length(objective)>0),
  state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN
    ('draft','ready','assigned','in_progress','blocked','submitted','under_review',
     'changes_requested','accepted','closed','failed','cancelled')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
  PRIMARY KEY(company_id,id),
  FOREIGN KEY(company_id,parent_id) REFERENCES tasks(company_id,id),
  FOREIGN KEY(company_id,accountable_appointment_id) REFERENCES appointments(company_id,id),
  FOREIGN KEY(company_id,assigned_appointment_id) REFERENCES appointments(company_id,id),
  CHECK(parent_id IS NULL OR parent_id <> id)
);
CREATE TABLE IF NOT EXISTS audit_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT NOT NULL REFERENCES companies(id),
  aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
  event_type TEXT NOT NULL, actor_ref TEXT NOT NULL, payload TEXT NOT NULL,
  occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS command_receipts (
  company_id TEXT NOT NULL REFERENCES companies(id), key TEXT NOT NULL,
  command_type TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL,
  PRIMARY KEY(company_id,key)
);
"""


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Store:
    def __init__(self, path: str | Path, *, fixture_bootstrap: bool = False):
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.fixture_bootstrap = fixture_bootstrap

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[None]:
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def audit(self, company: str, kind: str, item: str, event: str, actor: str, payload: dict):
        self.db.execute('INSERT INTO audit_events(company_id,aggregate_type,aggregate_id,event_type,actor_ref,payload,occurred_at) VALUES (?,?,?,?,?,?,?)',
                        (company,kind,item,event,actor,canonical(payload),
                         datetime.now(timezone.utc).isoformat()))

    def bootstrap(self, company: str, positions: list[tuple[str,str,str|None,str,str]]) -> Actor:
        """Fixture-only single-transaction setup, never a public provisioning API."""
        if not self.fixture_bootstrap:
            raise DomainError('BOOTSTRAP_DISABLED')
        with self.tx():
            self.db.execute('INSERT INTO companies(id) VALUES (?)',(company,))
            self.audit(company,'company',company,'company.provisioned','system:fixture',{})
            for pid,role,manager,agent,appt in positions:
                if (role=='core.ceo@1') != (manager is None):
                    raise DomainError('ORG_INVALID_ROOT')
                self.db.execute('INSERT INTO positions VALUES (?,?,?,?)',(company,pid,role,manager))
                self.db.execute('INSERT INTO agents VALUES (?,?,?)',(company,agent,'permanent'))
                self.db.execute('INSERT INTO appointments VALUES (?,?,?,?,?)',(company,appt,agent,pid,'active'))
                self.audit(company,'appointment',appt,'appointment.created','system:fixture',{'position':pid})
        return Actor(company,positions[0][3],positions[0][4])

    def require_actor(self, actor: Actor, company: str) -> None:
        if actor.company_id != company:
            raise DomainError('AUTH_TENANT_MISMATCH')
        row = self.db.execute('SELECT 1 FROM appointments WHERE company_id=? AND agent_id=? AND id=? AND status=?',
                              (company,actor.agent_id,actor.appointment_id,'active')).fetchone()
        if row is None:
            raise DomainError('AUTH_INVALID_APPOINTMENT')

    def receipt(self, company: str, key: str, command: str, digest: str):
        old=self.db.execute('SELECT * FROM command_receipts WHERE company_id=? AND key=?',(company,key)).fetchone()
        if old is None:
            return None
        if old['command_type']!=command or old['digest']!=digest:
            raise DomainError('IDEMPOTENCY_CONFLICT')
        return json.loads(old['result'])

    def save_receipt(self, company: str, key: str, command: str, digest: str, result: dict):
        self.db.execute('INSERT INTO command_receipts VALUES (?,?,?,?,?)',
                        (company,key,command,digest,canonical(result)))

    def create_task(self, actor: Actor, task_id: str, objective: str,
                    accountable: str, assigned: str | None, key: str,
                    parent_id: str | None = None) -> dict:
        if not task_id or not objective.strip() or not key:
            raise DomainError('SCHEMA_REQUIRED_FIELD')
        company=actor.company_id
        data={'actor':actor.__dict__,'task':task_id,'objective':objective,
              'accountable':accountable,'assigned':assigned,'parent':parent_id}
        digest=hashlib.sha256(canonical(data).encode()).hexdigest()
        with self.tx():
            self.require_actor(actor,company)
            cached=self.receipt(company,key,'CreateTask',digest)
            if cached is not None:
                return cached
            for appointment in (accountable,assigned):
                if appointment is not None and self.db.execute(
                    'SELECT 1 FROM appointments WHERE company_id=? AND id=? AND status=?',
                    (company,appointment,'active')).fetchone() is None:
                    raise DomainError('AUTH_INVALID_TASK_APPOINTMENT')
            # Cross-appointment work must go through the future Delegation contract.
            if accountable != actor.appointment_id or assigned not in (None, actor.appointment_id):
                raise DomainError('AUTH_DELEGATION_REQUIRED')
            if parent_id is not None:
                if self.db.execute('SELECT 1 FROM tasks WHERE company_id=? AND id=?',
                                   (company,parent_id)).fetchone() is None:
                    raise DomainError('REFERENCE_PARENT_NOT_FOUND')
                # Child tasks are created only after a scoped Delegation is accepted.
                raise DomainError('AUTH_DELEGATION_REQUIRED')
            try:
                self.db.execute('INSERT INTO tasks(company_id,id,parent_id,accountable_appointment_id,assigned_appointment_id,objective) VALUES (?,?,?,?,?,?)',
                                (company,task_id,parent_id,accountable,assigned,objective))
            except sqlite3.IntegrityError as e:
                raise DomainError('SCHEMA_CONSTRAINT') from e
            result={'id':task_id,'company_id':company,'state':'draft','revision':1}
            self.audit(company,'task',task_id,'task.created',f'{actor.agent_id}@{actor.appointment_id}',{'parent':parent_id})
            self.save_receipt(company,key,'CreateTask',digest,result)
            return result

    def transition_task(self, actor: Actor, task_id: str, target: str,
                        expected_revision: int, key: str) -> dict:
        if not task_id or not key:
            raise DomainError('SCHEMA_REQUIRED_FIELD')
        company=actor.company_id
        data={'actor':actor.__dict__,'task':task_id,'target':target,'revision':expected_revision}
        digest=hashlib.sha256(canonical(data).encode()).hexdigest()
        with self.tx():
            self.require_actor(actor,company)
            cached=self.receipt(company,key,'TransitionTask',digest)
            if cached is not None:
                return cached
            task=self.db.execute('SELECT * FROM tasks WHERE company_id=? AND id=?',
                                 (company,task_id)).fetchone()
            if task is None:
                raise DomainError('REFERENCE_TASK_NOT_FOUND')
            if task['accountable_appointment_id']!=actor.appointment_id:
                raise DomainError('AUTH_NOT_ACCOUNTABLE')
            if task['revision']!=expected_revision:
                raise DomainError('STALE_REVISION')
            # Intentional PR-1 subset: never accept/close without report/review guards.
            if (task['state'],target) not in {('draft','ready'),('ready','assigned'),
                                             ('draft','cancelled'),('ready','cancelled')}:
                raise DomainError('STATE_TRANSITION_NOT_IMPLEMENTED_OR_FORBIDDEN')
            if target=='assigned' and task['assigned_appointment_id'] is None:
                raise DomainError('STATE_ASSIGNMENT_REQUIRED')
            self.db.execute('UPDATE tasks SET state=?,revision=revision+1 WHERE company_id=? AND id=? AND revision=?',
                            (target,company,task_id,expected_revision))
            result={'id':task_id,'company_id':company,'state':target,'revision':expected_revision+1}
            self.audit(company,'task',task_id,'task.'+target,f'{actor.agent_id}@{actor.appointment_id}',
                       {'from':task['state'],'to':target})
            self.save_receipt(company,key,'TransitionTask',digest,result)
            return result

    def task(self, actor: Actor, task_id: str) -> dict:
        self.require_actor(actor,actor.company_id)
        row=self.db.execute('SELECT * FROM tasks WHERE company_id=? AND id=?',
                            (actor.company_id,task_id)).fetchone()
        if row is None:
            raise DomainError('REFERENCE_TASK_NOT_FOUND')
        return dict(row)

    def events(self, actor: Actor) -> list[dict]:
        self.require_actor(actor,actor.company_id)
        return [dict(x) for x in self.db.execute(
            'SELECT * FROM audit_events WHERE company_id=? ORDER BY event_id',
            (actor.company_id,)).fetchall()]
