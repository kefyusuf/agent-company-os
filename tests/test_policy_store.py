"""Policy resolver tests against the repository Store and isolated SQLite state."""
import hashlib
import json
import sqlite3
import unittest
import tempfile
from pathlib import Path
from datetime import datetime,timedelta,timezone
from company_os.store import Store,Actor,DomainError,canonical
from company_os.policy_store import SQLitePolicyResolver
from company_os.operations import ExternalOperationManager, ScriptedExternalAdapter
from company_os.workflow import Workflows

NOW=datetime.now(timezone.utc)

class SnapshotResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Store(Path(self.tmp.name)/'resolver.db',fixture_bootstrap=True)
        d=self.s.db
        d.execute('INSERT INTO companies(id,state) VALUES (?,?)',('C','active'))
        d.execute('INSERT INTO companies(id,state) VALUES (?,?)',('D','active'))
        for c in ('C','D'):
            d.execute('INSERT INTO positions VALUES (?,?,?,?)',(c,'P','role.engineer@1',None))
            d.execute('INSERT INTO agents VALUES (?,?,?)',(c,'AGT','permanent'))
            d.execute('INSERT INTO appointments VALUES (?,?,?,?,?)',(c,'APT','AGT','P','active'))
            d.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)',(c,'T',None,'APT','APT','demo','assigned',1))
        self.workflow=Workflows(self.s)
        self.r=SQLitePolicyResolver(self.s)
        self.actor=Actor('C','AGT','APT')
        self.action='task.start'
        self.payload={'task_id':'T','expected_revision':1}
        self.rule={'action':self.action,'eligible_roles':['role.engineer@1'],
                   'required_capabilities':['cap.work'], 'allowed_task_states':['assigned'],
                   'allowed_company_states':['active'], 'task_required':True,
                   'approval_required':False,'risk_level':'L1'}
        d.execute('INSERT INTO company_policy_versions VALUES (?,?,?)',('C','pol@1',1))
        self.set_rule(self.rule)
        d.execute('INSERT INTO appointment_capabilities VALUES (?,?,?)',('C','APT','cap.work'))
        d.execute('INSERT INTO policy_grants VALUES (?,?,?,?,?,?,?,?,?)',('C','G','APT','T',self.action,'task:T',
            (NOW+timedelta(hours=1)).isoformat(),0,1))
        d.execute('INSERT INTO policy_budgets VALUES (?,?)',('C',10))

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def in_tx(self, fn):
        with self.s.tx():
            return fn()

    def set_rule(self,rule,cost=0):
        self.s.db.execute('INSERT OR REPLACE INTO company_policy_rules VALUES (?,?,?,?,?)',
                ('C','pol@1',rule['action'],json.dumps(rule),cost))

    def enforce(self,**payload):
        with self.s.tx():
            self.r.enforce(self.actor,self.action,{**self.payload,**payload})

    def rejected(self,reason,call=None):
        with self.assertRaises(DomainError) as e:
            (call or self.enforce)()
        self.assertEqual(e.exception.code,reason)

    def test_happy_path(self): self.enforce()

    def test_forged_agent(self):
        self.rejected('AUTH_INVALID_APPOINTMENT',lambda:self.in_tx(lambda:self.r.enforce(Actor('C','DIFFERENT','APT'),self.action,self.payload)))

    def test_no_active_policy(self):
        self.s.db.execute('UPDATE company_policy_versions SET active=0')
        self.rejected('POLICY_NO_ACTIVE_VERSION')

    def test_unknown_action(self):
        self.rejected('POLICY_UNKNOWN_ACTION',lambda:self.in_tx(lambda:self.r.enforce(self.actor,'unlisted',self.payload)))

    def test_grant_does_not_carry_across_policy_version(self):
        self.enforce()
        self.s.db.execute("UPDATE company_policy_versions SET active=0 WHERE company_id='C'")
        self.s.db.execute("INSERT INTO company_policy_versions VALUES (?,?,?)",('C','pol@2',1))
        self.s.db.execute("INSERT INTO company_policy_rules VALUES (?,?,?,?,?)",
            ('C','pol@2',self.action,json.dumps(self.rule),0))
        # Same company, action, task and appointment; still no implicit re-grant.
        self.rejected('AUTH_GRANT_SCOPE')
        self.s.db.execute("INSERT INTO policy_grants VALUES (?,?,?,?,?,?,?,?,?)",
            ('C','G-POL2','APT','T',self.action,'task:T',
             (NOW+timedelta(hours=1)).isoformat(),0,1))
        self.enforce()

    def test_grant_issued_without_active_policy_never_becomes_valid(self):
        self.s.db.execute("UPDATE company_policy_versions SET active=0 WHERE company_id='C'")
        self.s.db.execute("INSERT INTO policy_grants VALUES (?,?,?,?,?,?,?,?,?)",
            ('C','G-UNBOUND','APT','T',self.action,'task:T',
             (NOW+timedelta(hours=1)).isoformat(),0,1))
        self.s.db.execute("UPDATE policy_grants SET active=0 WHERE id='G' AND company_id='C'")
        self.s.db.execute("UPDATE company_policy_versions SET active=1 WHERE company_id='C'")
        self.rejected('AUTH_GRANT_SCOPE')

    def test_policy_grant_version_provenance_is_immutable(self):
        self.assertEqual(self.s.db.execute(
            "SELECT policy_version FROM policy_grant_versions WHERE company_id='C' AND grant_id='G'"
        ).fetchone()['policy_version'],'pol@1')
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute(
                "UPDATE policy_grant_versions SET policy_version=? WHERE company_id=? AND grant_id=?",
                ('pol@1','C','G'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute(
                "DELETE FROM policy_grant_versions WHERE company_id=? AND grant_id=?",
                ('C','G'))
        self.enforce()

    def test_missing_grant(self):
        self.s.db.execute('UPDATE policy_grants SET active=0')
        self.rejected('AUTH_GRANT_SCOPE')

    def test_ended_appointment(self):
        self.s.db.execute('UPDATE appointments SET status=? WHERE company_id=?',('ended','C'))
        self.rejected('AUTH_INVALID_APPOINTMENT')

    def test_wrong_role(self):
        self.s.db.execute('UPDATE positions SET role_ref=? WHERE company_id=?',('other.role','C'))
        self.rejected('AUTH_ROLE_NOT_ALLOWED')

    def test_missing_capability(self):
        self.s.db.execute('DELETE FROM appointment_capabilities')
        self.rejected('AUTH_CAPABILITY_MISSING')

    def test_stale_revision(self):
        self.rejected('STALE_REVISION',lambda:self.enforce(expected_revision=2))

    def test_wrong_task_state(self):
        self.s.db.execute('UPDATE tasks SET state=? WHERE company_id=?',('cancelled','C'))
        self.rejected('AUTH_TASK_STATE')

    def test_paused_company(self):
        self.s.db.execute('UPDATE companies SET state=? WHERE id=?',('paused','C'))
        self.rejected('AUTH_COMPANY_STATE')

    def test_ambiguous_grants(self):
        self.s.db.execute('INSERT INTO policy_grants VALUES (?,?,?,?,?,?,?,?,?)',('C','G2','APT','T',self.action,'task:T',None,0,1))
        self.rejected('POLICY_AMBIGUOUS_GRANT')

    def test_expired_grant(self):
        self.s.db.execute('UPDATE policy_grants SET expires_at=? WHERE company_id=?',((NOW-timedelta(days=1)).isoformat(),'C'))
        self.rejected('AUTH_GRANT_EXPIRED')

    def test_positive_cost_reserves_budget(self):
        self.set_rule(self.rule,cost=4)
        self.s.db.execute('UPDATE policy_grants SET max_cost_units=10 WHERE company_id=?',('C',))
        self.enforce()
        remaining=self.s.db.execute(
            "SELECT remaining_units FROM policy_budgets WHERE company_id='C'").fetchone()['remaining_units']
        self.assertEqual(remaining,6)

    def test_exact_budget_allowed(self):
        self.set_rule(self.rule,cost=10)
        self.s.db.execute('UPDATE policy_grants SET max_cost_units=10 WHERE company_id=?',('C',))
        self.enforce()
        remaining=self.s.db.execute(
            "SELECT remaining_units FROM policy_budgets WHERE company_id='C'").fetchone()['remaining_units']
        self.assertEqual(remaining,0)

    def test_budget_exceeded_denied(self):
        self.set_rule(self.rule,cost=11)
        self.rejected('BUDGET_EXCEEDED')
        remaining=self.s.db.execute(
            "SELECT remaining_units FROM policy_budgets WHERE company_id='C'").fetchone()['remaining_units']
        self.assertEqual(remaining,10)

    def test_budget_missing_row_denied(self):
        self.set_rule(self.rule,cost=1)
        self.s.db.execute('DELETE FROM policy_budgets')
        self.rejected('BUDGET_NO_ROW')

    def test_budget_reservation_refunded_on_downstream_denial(self):
        self.set_rule(self.rule,cost=4)
        self.s.db.execute('UPDATE policy_grants SET active=0')
        self.rejected('AUTH_GRANT_SCOPE')
        remaining=self.s.db.execute(
            "SELECT remaining_units FROM policy_budgets WHERE company_id='C'").fetchone()['remaining_units']
        self.assertEqual(remaining,10)

    def test_same_identity_independent_review_denied(self):
        rule={**self.rule,'action':'report.review',
              'allowed_task_states':['under_review'],
              'independent_reviewer_required':True}
        self.set_rule(rule)
        self.s.db.execute('UPDATE tasks SET state=? WHERE company_id=?',('under_review','C'))
        self.s.db.execute('''INSERT INTO reports (company_id,id,version,task_id,
             author_appointment_id,author_agent_id,content) VALUES (?,?,?,?,?,?,?)''',
             ('C','R',1,'T','APT','AGT','Fixture author report'))
        self.s.db.execute('INSERT INTO policy_grants VALUES (?,?,?,?,?,?,?,?,?)',
           ('C','REV','APT','T','report.review','task:T',None,0,1))
        self.rejected('CONFLICT_INDEPENDENT_REVIEW',lambda:self.in_tx(lambda:
          self.r.enforce(self.actor,'report.review',
            {'report_id':'R','version':1,'review_id':'RV','decision':'accepted','reason':'x'})))

    def test_different_identity_not_proven_independent_without_lineage(self):
        rule={**self.rule,'action':'report.review',
              'allowed_task_states':['under_review'],
              'independent_reviewer_required':True}
        self.set_rule(rule)
        self.s.db.execute('UPDATE tasks SET state=? WHERE company_id=?',('under_review','C'))
        self.s.db.execute('INSERT INTO positions VALUES (?,?,?,?)',('C','P-OTHER','role.engineer@1',None))
        self.s.db.execute('INSERT INTO agents VALUES (?,?,?)',('C','AGT-OTHER','permanent'))
        self.s.db.execute('INSERT INTO appointments VALUES (?,?,?,?,?)',
                         ('C','APT-OTHER','AGT-OTHER','P-OTHER','active'))
        self.s.db.execute('''INSERT INTO reports (company_id,id,version,task_id,
             author_appointment_id,author_agent_id,content) VALUES (?,?,?,?,?,?,?)''',
             ('C','R',1,'T','APT-OTHER','AGT-OTHER','Independent author fixture'))
        self.s.db.execute('INSERT INTO policy_grants VALUES (?,?,?,?,?,?,?,?,?)',
           ('C','REV','APT','T','report.review','task:T',None,0,1))
        self.rejected('POLICY_REVIEW_LINEAGE_UNAVAILABLE',lambda:self.in_tx(lambda:
          self.r.enforce(self.actor,'report.review',
            {'report_id':'R','version':1,'review_id':'RV','decision':'accepted','reason':'x'})))

    def _review_setup(self):
        rule={**self.rule,'action':'report.review',
              'allowed_task_states':['under_review'],
              'independent_reviewer_required':True}
        self.set_rule(rule)
        self.s.db.execute('UPDATE tasks SET state=? WHERE company_id=?',('under_review','C'))
        self.s.db.execute('INSERT INTO positions VALUES (?,?,?,?)',('C','P-OTHER','role.engineer@1',None))
        self.s.db.execute('INSERT INTO agents VALUES (?,?,?)',('C','AGT-OTHER','permanent'))
        self.s.db.execute('INSERT INTO appointments VALUES (?,?,?,?,?)',
                         ('C','APT-OTHER','AGT-OTHER','P-OTHER','active'))
        self.s.db.execute('INSERT INTO agents VALUES (?,?,?)',('C','AGT-DELEG','permanent'))
        self.s.db.execute('''INSERT INTO reports (company_id,id,version,task_id,
             author_appointment_id,author_agent_id,content) VALUES (?,?,?,?,?,?,?)''',
             ('C','R',1,'T','APT-OTHER','AGT-OTHER','Independent author fixture'))
        self.s.db.execute('INSERT INTO policy_grants VALUES (?,?,?,?,?,?,?,?,?)',
           ('C','REV','APT','T','report.review','task:T',None,0,1))

    def test_attested_lineage_allows_independent_review(self):
        self._review_setup()
        self.s.db.execute('''INSERT INTO report_contributor_lineage
            (company_id,report_id,report_version,contributor_agent_id,
             contribution_kind,attested_by_appointment_id,recorded_at)
            VALUES (?,?,?,?,?,?,?)''',
            ('C','R',1,'AGT-OTHER','author','APT-OTHER',NOW.isoformat()))
        self.s.db.execute('''INSERT INTO report_contributor_lineage
            (company_id,report_id,report_version,contributor_agent_id,
             contribution_kind,attested_by_appointment_id,recorded_at)
            VALUES (?,?,?,?,?,?,?)''',
            ('C','R',1,'AGT-DELEG','delegator','APT-OTHER',NOW.isoformat()))
        with self.s.tx():
            self.r.enforce(self.actor,'report.review',
              {'report_id':'R','version':1,'review_id':'RV','decision':'accepted','reason':'x'})

    def test_attested_lineage_contributor_reviewer_denied(self):
        self._review_setup()
        self.s.db.execute('''INSERT INTO report_contributor_lineage
            (company_id,report_id,report_version,contributor_agent_id,
             contribution_kind,attested_by_appointment_id,recorded_at)
            VALUES (?,?,?,?,?,?,?)''',
            ('C','R',1,'AGT-OTHER','author','APT-OTHER',NOW.isoformat()))
        # The reviewer's own identity is inside the attested contributor set.
        self.s.db.execute('''INSERT INTO report_contributor_lineage
            (company_id,report_id,report_version,contributor_agent_id,
             contribution_kind,attested_by_appointment_id,recorded_at)
            VALUES (?,?,?,?,?,?,?)''',
            ('C','R',1,'AGT','implementer','APT-OTHER',NOW.isoformat()))
        self.rejected('CONFLICT_INDEPENDENT_REVIEW',lambda:self.in_tx(lambda:
          self.r.enforce(self.actor,'report.review',
            {'report_id':'R','version':1,'review_id':'RV','decision':'accepted','reason':'x'})))

    def test_report_lineage_is_immutable(self):
        self.s.db.execute('INSERT INTO agents VALUES (?,?,?)',('C','AGT-AUTH','permanent'))
        self.s.db.execute('''INSERT INTO reports (company_id,id,version,task_id,
             author_appointment_id,author_agent_id,content) VALUES (?,?,?,?,?,?,?)''',
             ('C','R',1,'T','APT','AGT-AUTH','Lineage fixture'))
        self.s.db.execute('''INSERT INTO report_contributor_lineage
            (company_id,report_id,report_version,contributor_agent_id,
             contribution_kind,attested_by_appointment_id,recorded_at)
            VALUES (?,?,?,?,?,?,?)''',
            ('C','R',1,'AGT-AUTH','author','APT',NOW.isoformat()))
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute('''UPDATE report_contributor_lineage
                SET contribution_kind=? WHERE company_id=?''',('delegator','C'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute('''DELETE FROM report_contributor_lineage
                WHERE company_id=?''',('C',))

    def setup_delegation_accept(self):
        rule={**self.rule,'action':'delegation.accept',
              'allowed_task_states':['in_progress']}
        self.set_rule(rule)
        self.s.db.execute('UPDATE tasks SET state=?,revision=? WHERE company_id=?',
                          ('in_progress',4,'C'))
        self.s.db.execute('''INSERT INTO delegations
             (company_id,id,parent_task_id,offered_by_appointment_id,
              target_appointment_id,child_task_id,objective,state,revision)
              VALUES (?,?,?,?,?,?,?,?,?)''',
              ('C','OFFER','T','APT','APT','CHILD','Safe fixture','offered',1))
        self.s.db.execute('INSERT INTO policy_grants VALUES (?,?,?,?,?,?,?,?,?)',
              ('C','GRANT-ACCEPT','APT','T','delegation.accept','task:T',None,0,1))

    def test_offer_accept_compares_delegation_revision_not_parent_revision(self):
        self.setup_delegation_accept()
        with self.s.tx():
            self.r.enforce(self.actor,'delegation.accept',
                           {'offer_id':'OFFER','expected_revision':1})

    def test_stale_offer_revision_denied_independent_of_parent_revision(self):
        self.setup_delegation_accept()
        self.rejected('STALE_REVISION',lambda:self.in_tx(lambda:
           self.r.enforce(self.actor,'delegation.accept',
                          {'offer_id':'OFFER','expected_revision':2})))

    def test_malformed_boolean_policy_rule_denied(self):
        rule={**self.rule,'approval_required':'false'}
        self.set_rule(rule)
        self.rejected('POLICY_INVALID_STORED_RULE')

    def test_unbounded_rule_rejected(self):
        rule={**self.rule,'allowed_task_states':[]}
        self.set_rule(rule)
        self.rejected('POLICY_INVALID_STORED_RULE')

    def test_unsupported_action_routed_is_denied(self):
        self.rejected('POLICY_UNKNOWN_ACTION',lambda:self.in_tx(lambda:self.r.enforce(self.actor,'policy.modify',{})))

    def test_other_company_no_policy(self):
        self.rejected('POLICY_NO_ACTIVE_VERSION',lambda:self.in_tx(lambda:self.r.enforce(Actor('D','AGT','APT'),self.action,self.payload)))

    def test_scope_cannot_union_foreign_appointment(self):
        self.s.db.execute('INSERT INTO positions VALUES (?,?,?,?)',('C','P2','role.engineer@1',None))
        self.s.db.execute('INSERT INTO appointments VALUES (?,?,?,?,?)',('C','APT2','AGT','P2','active'))
        self.s.db.execute('UPDATE policy_grants SET appointment_id=?',('APT2',))
        self.rejected('AUTH_GRANT_SCOPE')

    def test_tenant_missing_task_denied(self):
        self.rejected('REFERENCE_TASK_NOT_FOUND',lambda:self.enforce(task_id='UNKNOWN'))

    def test_requires_transaction(self):
        self.rejected('POLICY_REQUIRES_TRANSACTION',lambda:self.r.enforce(self.actor,self.action,self.payload))

    def test_approval_required_fails_closed(self):
        rule={**self.rule,'approval_required':True,'risk_level':'L3'}
        self.set_rule(rule)
        self.rejected('AUTH_EXACT_APPROVAL_REQUIRED')

    def test_approved_snapshot_resolves_with_approval_id(self):
        rule={**self.rule,'approval_required':True,'risk_level':'L3'}
        self.set_rule(rule)
        intent='sha256:'+hashlib.sha256(canonical({
            'company_id':'C','action':self.action,'task_id':'T',
            'resource':'task:T','payload':self.payload}).encode()).hexdigest()
        self.s.db.execute('''INSERT INTO policy_approvals (company_id,id,task_id,action,
             resource_id,payload_sha256,target_revision,policy_version,expires_at,
             status,approver_kind,approver_id,consumed_by_receipt)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          ('C','APR','T',self.action,'task:T',intent,1,'pol@1',
           (NOW+timedelta(minutes=5)).isoformat(),'approved','human','OWNER',None))
        with self.s.tx():
            info=self.r.enforce(self.actor,self.action,self.payload)
        self.assertEqual(info['approval_id'],'APR')
        status=self.s.db.execute(
            "SELECT status FROM policy_approvals WHERE company_id='C' AND id='APR'").fetchone()['status']
        # Consumption is the gateway's job, inside the command transaction.
        self.assertEqual(status,'approved')

    def test_approval_wrong_payload(self):
        rule={**self.rule,'approval_required':True,'risk_level':'L3'}
        self.set_rule(rule)
        self.s.db.execute('''INSERT INTO policy_approvals (company_id,id,task_id,action,
             resource_id,payload_sha256,target_revision,policy_version,expires_at,
             status,approver_kind,approver_id,consumed_by_receipt)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          ('C','APR','T',self.action,'task:T','sha256:'+'0'*64,1,'pol@1',
           (NOW+timedelta(minutes=5)).isoformat(),'approved','human','OWNER',None))
        self.rejected('AUTH_EXACT_APPROVAL_REQUIRED')

    def test_corrupt_rule_rejected(self):
        self.s.db.execute('UPDATE company_policy_rules SET rule_json=?',('{not json',))
        self.rejected('POLICY_INVALID_STORED_RULE')

    def test_grant_bad_timestamp_denied(self):
        self.s.db.execute('UPDATE policy_grants SET expires_at=?',('not-date',))
        self.rejected('POLICY_INVALID_SNAPSHOT_TIME')

    def test_active_different_role_no_union(self):
        self.s.db.execute('INSERT INTO positions VALUES (?,?,?,?)',('C','P2','role.other@1',None))
        self.s.db.execute('INSERT INTO appointments VALUES (?,?,?,?,?)',('C','APT2','AGT','P2','active'))
        self.rejected('AUTH_ROLE_NOT_ALLOWED',lambda: self._enforce_other_appt())

    def _enforce_other_appt(self):
        with self.s.tx():
            self.r.enforce(Actor('C','AGT','APT2'),self.action,self.payload)

class ExternalOperationTests(unittest.TestCase):
    """M5.4-D1: attempt ledger, scripted adapter seam and FIX-10 guard."""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Store(Path(self.tmp.name)/'operations.db',fixture_bootstrap=True)
        d=self.s.db
        d.execute('INSERT INTO companies(id,state) VALUES (?,?)',('C','active'))
        d.execute('INSERT INTO positions VALUES (?,?,?,?)',('C','P','role.engineer@1',None))
        d.execute('INSERT INTO agents VALUES (?,?,?)',('C','AGT','permanent'))
        d.execute('INSERT INTO appointments VALUES (?,?,?,?,?)',('C','APT','AGT','P','active'))
        # Policy tables (capabilities, grants, rules, budgets) come from the
        # resolver's schema; instantiate it before inserting policy rows.
        self.r=SQLitePolicyResolver(self.s)
        d.execute('INSERT INTO appointment_capabilities VALUES (?,?,?)',('C','APT','cap.dispatch'))
        d.execute('INSERT INTO company_policy_versions VALUES (?,?,?)',('C','pol@1',1))
        self.rule={'action':'operation.dispatch','eligible_roles':['role.engineer@1'],
                   'required_capabilities':['cap.dispatch'],'allowed_task_states':[],
                   'allowed_company_states':['active'],'task_required':False,
                   'approval_required':False,'risk_level':'L2'}
        d.execute('INSERT INTO company_policy_rules VALUES (?,?,?,?,?)',
                  ('C','pol@1','operation.dispatch',json.dumps(self.rule),3))
        d.execute('INSERT INTO policy_grants VALUES (?,?,?,?,?,?,?,?,?)',
                  ('C','G-EXT','APT',None,'operation.dispatch','external:publish',
                   (NOW+timedelta(hours=1)).isoformat(),5,1))
        d.execute('INSERT INTO policy_budgets VALUES (?,?)',('C',10))
        self.adapter=ScriptedExternalAdapter()
        self.manager=ExternalOperationManager(self.s,self.adapter)
        self.actor=Actor('C','AGT','APT')
        self.payload={'attempt_id':'ATT-1','target_action':'external.publish',
                      'target_resource':'external:publish','target_payload':{'note':'x'}}

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def set_rule(self,rule,cost=3):
        self.s.db.execute('INSERT OR REPLACE INTO company_policy_rules VALUES (?,?,?,?,?)',
                          ('C','pol@1',rule['action'],json.dumps(rule),cost))

    def _run(self,payload,key='k1'):
        with self.s.tx():
            info=self.r.enforce(self.actor,'operation.dispatch',payload)
            return self.manager.dispatch(self.actor,payload,info,key)

    def attempts(self):
        return self.s.db.execute('SELECT count(*) FROM operation_attempts').fetchone()[0]

    def budget(self):
        return self.s.db.execute(
            "SELECT remaining_units FROM policy_budgets WHERE company_id='C'").fetchone()['remaining_units']

    def test_dispatch_succeeded_commits_and_audits(self):
        self.adapter.script('external.publish','succeeded')
        result=self._run(self.payload)
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(result['attempt_id'],'ATT-1')
        row=self.s.db.execute('''SELECT state,adapter_ref,reserved_cost_units,guard_key
            FROM operation_attempts WHERE company_id='C' AND id='ATT-1' ''').fetchone()
        self.assertEqual(row['state'],'succeeded')
        self.assertIsNotNone(row['adapter_ref'])
        self.assertEqual(row['reserved_cost_units'],3)
        self.assertEqual(len(row['guard_key']),64)
        for event in ('operation.authorized','operation.dispatched','operation.succeeded'):
            count=self.s.db.execute('SELECT count(*) FROM audit_events WHERE event_type=?',
                                    (event,)).fetchone()[0]
            self.assertEqual(count,1)
        self.assertEqual(self.budget(),7)

    def test_dispatch_failed_refunds_budget(self):
        self.adapter.script('external.publish','failed')
        result=self._run(self.payload)
        self.assertEqual(result['state'],'failed')
        self.assertEqual(self.budget(),10)
        refunded=self.s.db.execute("SELECT count(*) FROM audit_events WHERE event_type='operation.cost_refunded'").fetchone()[0]
        self.assertEqual(refunded,1)

    def test_dispatch_unknown_holds_budget(self):
        self.adapter.script('external.publish','unknown')
        result=self._run(self.payload)
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(self.budget(),7)

    def test_unscripted_adapter_error_rolls_back_everything(self):
        with self.assertRaises(RuntimeError):
            self._run(self.payload)
        self.assertEqual(self.attempts(),0)
        self.assertEqual(self.budget(),10)

    def test_attempt_binding_is_immutable(self):
        self.adapter.script('external.publish','succeeded')
        self._run(self.payload)
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute('''UPDATE operation_attempts SET action='other'
                WHERE company_id='C' AND id='ATT-1' ''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute('''UPDATE operation_attempts SET reserved_cost_units=0
                WHERE company_id='C' AND id='ATT-1' ''')

    def test_duplicate_attempt_id_denied(self):
        self.adapter.script('external.publish','succeeded')
        self._run(self.payload,key='first')
        with self.assertRaises(DomainError) as caught:
            self._run(self.payload,key='second')
        self.assertEqual(caught.exception.code,'CONFLICT_ATTEMPT_EXISTS')
        self.assertEqual(self.attempts(),1)

    def test_unknown_blocks_redispatch_with_same_intent(self):
        self.adapter.script('external.publish','unknown')
        self._run(self.payload,key='first')
        retry={'attempt_id':'ATT-2','target_action':'external.publish',
               'target_resource':'external:publish','target_payload':{'note':'x'}}
        with self.assertRaises(DomainError) as caught:
            self._run(retry,key='retry')
        self.assertEqual(caught.exception.code,'OPERATION_UNRESOLVED_ATTEMPT_EXISTS')
        self.assertEqual(self.attempts(),1)
        self.assertEqual(self.budget(),7)

    def test_policy_denial_leaves_no_attempt(self):
        self.s.db.execute('UPDATE policy_grants SET active=0')
        with self.assertRaises(DomainError):
            self._run(self.payload)
        self.assertEqual(self.attempts(),0)
        self.assertEqual(self.budget(),10)

    def test_paused_company_denies_dispatch(self):
        self.s.db.execute("UPDATE companies SET state='paused' WHERE id='C'")
        with self.assertRaises(DomainError):
            self._run(self.payload)
        self.assertEqual(self.attempts(),0)

    def test_approval_binding_recorded_on_attempt(self):
        rule={**self.rule,'approval_required':True,'risk_level':'L3'}
        self.set_rule(rule)
        self.s.db.execute('INSERT INTO policy_grants VALUES (?,?,?,?,?,?,?,?,?)',
                          ('C','G-APR','APT',None,'operation.dispatch','external:approval',
                           (NOW+timedelta(hours=1)).isoformat(),5,1))
        approval_payload={'attempt_id':'ATT-APR','target_action':'external.publish',
                          'target_resource':'external:approval','target_payload':{'n':1}}
        intent='sha256:'+hashlib.sha256(canonical({
            'company_id':'C','action':'operation.dispatch','task_id':None,
            'resource':'external:approval','payload':approval_payload}).encode('utf-8')).hexdigest()
        self.s.db.execute('''INSERT INTO policy_approvals (company_id,id,task_id,action,
             resource_id,payload_sha256,target_revision,policy_version,expires_at,
             status,approver_kind,approver_id,consumed_by_receipt)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('C','APR',None,'operation.dispatch','external:approval',intent,1,'pol@1',
             (NOW+timedelta(minutes=5)).isoformat(),'approved','human','OWNER',None))
        self.adapter.script('external.publish','succeeded')
        with self.s.tx():
            info=self.r.enforce(self.actor,'operation.dispatch',approval_payload)
            result=self.manager.dispatch(self.actor,approval_payload,info,'apr-key')
        self.assertEqual(result['state'],'succeeded')
        row=self.s.db.execute('''SELECT consumed_approval_id FROM operation_attempts
            WHERE company_id='C' AND id='ATT-APR' ''').fetchone()
        self.assertEqual(row['consumed_approval_id'],'APR')

    def _dispatch_unknown(self, key='d1'):
        self.adapter.script('external.publish','unknown')
        result=self._run(self.payload,key=key)
        ref=self.s.db.execute(
            "SELECT adapter_ref FROM operation_attempts WHERE company_id='C' AND id='ATT-1'").fetchone()['adapter_ref']
        return result,ref

    def _reconcile(self,attempt_id='ATT-1',key='rec-1'):
        with self.s.tx():
            return self.manager.reconcile(self.actor,{'attempt_id':attempt_id},key)

    def test_reconcile_unknown_resolves_succeeded(self):
        _,ref=self._dispatch_unknown()
        self.assertEqual(self.budget(),7)
        self.adapter.script_status(ref,'succeeded')
        result=self._reconcile()
        self.assertEqual(result['state'],'succeeded')
        row=self.s.db.execute("SELECT state FROM operation_attempts WHERE company_id='C' AND id='ATT-1'").fetchone()
        self.assertEqual(row['state'],'succeeded')
        self.assertEqual(self.budget(),7)

    def test_reconcile_resolved_failed_refunds_budget(self):
        _,ref=self._dispatch_unknown()
        self.adapter.script_status(ref,'failed')
        result=self._reconcile()
        self.assertEqual(result['state'],'failed')
        self.assertEqual(self.budget(),10)
        refunded=self.s.db.execute("SELECT count(*) FROM audit_events WHERE event_type='operation.cost_refunded'").fetchone()[0]
        self.assertEqual(refunded,1)

    def test_reconcile_unresolvable_marks_unresolved_terminal(self):
        self._dispatch_unknown()
        result=self._reconcile()
        self.assertEqual(result['state'],'unresolved')
        self.assertEqual(self.budget(),7)
        with self.assertRaises(DomainError) as caught:
            self._reconcile()
        self.assertEqual(caught.exception.code,'OPERATION_RECONCILE_NOT_ALLOWED')

    def test_reconcile_requires_unknown_state(self):
        self.adapter.script('external.publish','succeeded')
        self._run(self.payload,key='d1')
        with self.assertRaises(DomainError) as caught:
            self._reconcile()
        self.assertEqual(caught.exception.code,'OPERATION_RECONCILE_NOT_ALLOWED')

    def test_unresolved_blocks_redispatch(self):
        self._dispatch_unknown()
        self._reconcile()
        retry={'attempt_id':'ATT-2','target_action':'external.publish',
               'target_resource':'external:publish','target_payload':{'note':'x'}}
        with self.assertRaises(DomainError) as caught:
            self._run(retry,key='retry')
        self.assertEqual(caught.exception.code,'OPERATION_UNRESOLVED_ATTEMPT_EXISTS')
        self.assertEqual(self.attempts(),1)

    def test_reconcile_audits_transitions(self):
        _,ref=self._dispatch_unknown()
        self.adapter.script_status(ref,'failed')
        self._reconcile()
        for event in ('operation.reconciling','operation.failed'):
            count=self.s.db.execute('SELECT count(*) FROM audit_events WHERE event_type=?',
                                    (event,)).fetchone()[0]
            self.assertEqual(count,1)

if __name__ == '__main__':
    unittest.main()
