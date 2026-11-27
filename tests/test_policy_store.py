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
        """T-PR1 two-phase shape: authorize+commit, then effect+settlement."""
        with self.s.tx():
            info=self.r.enforce(self.actor,'operation.dispatch',payload)
            self.manager.authorize_dispatch(self.actor,payload,info,key)
        call=self.manager.execute_effect(self.actor,payload,info)
        with self.s.tx():
            return self.manager.settle_dispatch(self.actor,payload,call,key)

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

    def test_unscripted_effect_leaves_attempt_midflight_recoverable(self):
        # T-PR1: the effect error happens AFTER the authorization commit, so
        # the attempt rests at `authorized` with its budget held — exactly the
        # crash window `recover` exists for (G-T1/G-T2). Nothing is lost and
        # nothing is retried automatically.
        with self.assertRaises(RuntimeError):
            self._run(self.payload)
        self.assertEqual(self.attempts(),1)
        self.assertEqual(self.budget(),7)
        row=self.s.db.execute('''SELECT state,transport_key,adapter_ref
            FROM operation_attempts WHERE company_id='C' AND id='ATT-1' ''').fetchone()
        self.assertEqual(row['state'],'authorized')
        self.assertEqual(row['transport_key'],'IDEM-ATT-1')
        self.assertIsNone(row['adapter_ref'])

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
            self.manager.authorize_dispatch(self.actor,approval_payload,info,'apr-key')
        call=self.manager.execute_effect(self.actor,approval_payload,info)
        with self.s.tx():
            result=self.manager.settle_dispatch(self.actor,approval_payload,call,'apr-key')
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

class ReconciliationDecisionTests(unittest.TestCase):
    """M5.4-D3: human reconciliation decisions, reissue-as-new-attempt, guards."""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Store(Path(self.tmp.name)/'decisions.db',fixture_bootstrap=True)
        d=self.s.db
        d.execute('INSERT INTO companies(id,state) VALUES (?,?)',('C','active'))
        d.execute('INSERT INTO positions VALUES (?,?,?,?)',('C','P','role.engineer@1',None))
        d.execute('INSERT INTO agents VALUES (?,?,?)',('C','AGT','permanent'))
        d.execute('INSERT INTO appointments VALUES (?,?,?,?,?)',('C','APT','AGT','P','active'))
        self.r=SQLitePolicyResolver(self.s)
        d.execute('INSERT INTO appointment_capabilities VALUES (?,?,?)',('C','APT','cap.dispatch'))
        d.execute('INSERT INTO company_policy_versions VALUES (?,?,?)',('C','pol@1',1))
        base={'eligible_roles':['role.engineer@1'],'required_capabilities':['cap.dispatch'],
              'allowed_task_states':[],'allowed_company_states':['active'],
              'task_required':False,'approval_required':False,'risk_level':'L2'}
        for action,cost in (('operation.dispatch',3),('operation.reconcile',0),
                            ('operation.decide',0),('operation.reissue',3)):
            d.execute('INSERT INTO company_policy_rules VALUES (?,?,?,?,?)',
                      ('C','pol@1',action,json.dumps({**base,'action':action}),cost))
        for gid,action,resource in (('G-EXT','operation.dispatch','external:publish'),
                                    ('G-EXT-B','operation.dispatch','other'),
                                    ('G-REC','operation.reconcile','company:C'),
                                    ('G-DEC','operation.decide','company:C'),
                                    ('G-REI','operation.reissue','external:publish'),
                                    ('G-REI-B','operation.reissue','other')):
            d.execute('INSERT INTO policy_grants VALUES (?,?,?,?,?,?,?,?,?)',
                      ('C',gid,'APT',None,action,resource,
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

    def _dispatch(self,payload,key='k1'):
        """T-PR1 two-phase shape: authorize+commit, then effect+settlement."""
        with self.s.tx():
            info=self.r.enforce(self.actor,'operation.dispatch',payload)
            self.manager.authorize_dispatch(self.actor,payload,info,key)
        call=self.manager.execute_effect(self.actor,payload,info)
        with self.s.tx():
            return self.manager.settle_dispatch(self.actor,payload,call,key)

    def _reconcile(self,attempt_id='ATT-1',key='rec-1'):
        with self.s.tx():
            return self.manager.reconcile(self.actor,{'attempt_id':attempt_id},key)

    def _unresolved(self,key='d1'):
        self.adapter.script('external.publish','unknown')
        self._dispatch(self.payload,key=key)
        return self._reconcile(key='rec-'+key)

    def _decide(self,decision='reissue',decision_id='DEC-1',attempt_id='ATT-1',
                key='dec-1',expires=None):
        payload={'attempt_id':attempt_id,'decision_id':decision_id,'decision':decision,
                 'expires_at':expires or (NOW+timedelta(hours=1)).isoformat()}
        with self.s.tx():
            info=self.r.enforce(self.actor,'operation.decide',payload)
            return self.manager.decide(self.actor,payload,info,key)

    def _reissue(self,decision_id='DEC-1',new_attempt_id='ATT-2',key='rei-1',
                 attempt_id='ATT-1',effect=True):
        """T-PR1 two-phase reissue: consume+authorize commit, then effect."""
        payload={'attempt_id':attempt_id,'decision_id':decision_id,
                 'new_attempt_id':new_attempt_id,'target_action':'external.publish',
                 'target_resource':'external:publish','target_payload':{'note':'x'}}
        with self.s.tx():
            info=self.r.enforce(self.actor,'operation.reissue',payload)
            self.manager.reissue(self.actor,payload,info,key)
        phase2={'attempt_id':new_attempt_id,'target_action':'external.publish',
                'target_resource':'external:publish','target_payload':{'note':'x'}}
        call=self.manager.execute_effect(self.actor,phase2,info)
        with self.s.tx():
            return self.manager.settle_dispatch(self.actor,phase2,call,key)

    def _insert_decision(self,decision_id='DEC-X',attempt_id='ATT-1',decision='reissue',
                         expires=None):
        """Trusted fixture write; bypasses the gateway-time decide validations."""
        self.s.db.execute('''INSERT INTO operation_reconciliations VALUES
            (?,?,?,?,?,?,?,NULL,?)''',
            ('C',decision_id,attempt_id,decision,'APT','pol@1',
             expires or (NOW+timedelta(hours=1)).isoformat(),NOW.isoformat()))

    def attempts(self):
        return self.s.db.execute('SELECT count(*) FROM operation_attempts').fetchone()[0]

    def budget(self):
        return self.s.db.execute(
            "SELECT remaining_units FROM policy_budgets WHERE company_id='C'").fetchone()['remaining_units']

    def state_of(self,attempt_id):
        return self.s.db.execute(
            '''SELECT state FROM operation_attempts WHERE company_id='C' AND id=?''',
            (attempt_id,)).fetchone()['state']

    def test_decide_records_reissue_provenance(self):
        self._unresolved()
        result=self._decide()
        self.assertEqual(result,{'decision_id':'DEC-1','attempt_id':'ATT-1','decision':'reissue'})
        row=self.s.db.execute("SELECT * FROM operation_reconciliations WHERE id='DEC-1'").fetchone()
        self.assertEqual(row['decision'],'reissue')
        self.assertEqual(row['decided_by_appointment_id'],'APT')
        self.assertEqual(row['policy_version'],'pol@1')
        self.assertIsNone(row['consumed_by_attempt_id'])
        count=self.s.db.execute("SELECT count(*) FROM audit_events WHERE event_type='operation.decision_recorded'").fetchone()[0]
        self.assertEqual(count,1)

    def test_decide_records_abandon_provenance(self):
        self._unresolved()
        result=self._decide(decision='abandon',decision_id='DEC-A')
        self.assertEqual(result['decision'],'abandon')

    def test_decide_requires_unresolved_state(self):
        self.adapter.script('external.publish','unknown')
        self._dispatch(self.payload,key='d-unknown')
        with self.assertRaises(DomainError) as caught:
            self._decide()
        self.assertEqual(caught.exception.code,'OPERATION_DECIDE_NOT_ALLOWED')
        self.adapter.script('external.publish','succeeded')
        self._dispatch({'attempt_id':'ATT-OK','target_action':'external.publish',
                        'target_resource':'external:publish','target_payload':{'n':0}},
                       key='d-ok')
        with self.assertRaises(DomainError) as caught:
            self._decide(decision_id='DEC-OK',attempt_id='ATT-OK')
        self.assertEqual(caught.exception.code,'OPERATION_DECIDE_NOT_ALLOWED')

    def test_decide_rejects_invalid_decision_value(self):
        self._unresolved()
        with self.assertRaises(DomainError) as caught:
            self._decide(decision='retry')
        self.assertEqual(caught.exception.code,'OPERATION_DECISION_INVALID')

    def test_decide_rejects_past_expiry(self):
        self._unresolved()
        with self.assertRaises(DomainError) as caught:
            self._decide(expires=(NOW-timedelta(minutes=1)).isoformat())
        self.assertEqual(caught.exception.code,'OPERATION_DECISION_EXPIRED')

    def test_decide_rejects_naive_expiry(self):
        self._unresolved()
        naive=(NOW+timedelta(hours=1)).replace(tzinfo=None).isoformat()
        with self.assertRaises(DomainError) as caught:
            self._decide(expires=naive)
        self.assertEqual(caught.exception.code,'OPERATION_DECISION_INVALID_EXPIRY')

    def test_decide_rejects_second_pending_decision(self):
        self._unresolved()
        self._decide()
        with self.assertRaises(DomainError) as caught:
            self._decide(decision_id='DEC-2')
        self.assertEqual(caught.exception.code,'OPERATION_DECISION_ALREADY_PENDING')

    def test_decide_rejects_after_abandon(self):
        self._unresolved()
        self._decide(decision='abandon',decision_id='DEC-A')
        with self.assertRaises(DomainError) as caught:
            self._decide(decision_id='DEC-2')
        self.assertEqual(caught.exception.code,'OPERATION_ATTEMPT_ABANDONED')

    def test_decide_rejects_duplicate_decision_id(self):
        self._unresolved()
        self._decide()
        self.adapter.script('external.publish','unknown')
        self._dispatch({'attempt_id':'ATT-B','target_action':'external.publish',
                        'target_resource':'other','target_payload':{'n':0}},key='d-b')
        self._reconcile(attempt_id='ATT-B',key='rec-b')
        with self.assertRaises(DomainError) as caught:
            self._decide(attempt_id='ATT-B')
        self.assertEqual(caught.exception.code,'CONFLICT_DECISION_EXISTS')

    def test_reissue_creates_new_attempt_and_clears_guard(self):
        self._unresolved()
        self.assertEqual(self.budget(),7)
        self._decide()
        self.adapter.script('external.publish','succeeded')
        result=self._reissue()
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(result['attempt_id'],'ATT-2')
        self.assertEqual(self.state_of('ATT-1'),'unresolved')
        self.assertEqual(self.state_of('ATT-2'),'succeeded')
        self.assertEqual(self.attempts(),2)
        consumed=self.s.db.execute("SELECT consumed_by_attempt_id FROM operation_reconciliations WHERE id='DEC-1'").fetchone()['consumed_by_attempt_id']
        self.assertEqual(consumed,'ATT-2')
        consumed_audits=self.s.db.execute("SELECT count(*) FROM audit_events WHERE event_type='operation.reissue_decision_consumed'").fetchone()[0]
        self.assertEqual(consumed_audits,1)
        # The original hold stays (unresolved never settles); the reissue's
        # own reservation is consumed by the succeeded new attempt.
        self.assertEqual(self.budget(),4)

    def test_reissue_failed_refunds_new_reservation_only(self):
        self._unresolved()
        self._decide()
        self.adapter.script('external.publish','failed')
        result=self._reissue()
        self.assertEqual(result['state'],'failed')
        # Original 3 stay held (unresolved); the reissue reservation refunds.
        self.assertEqual(self.budget(),7)
        refunded=self.s.db.execute("SELECT count(*) FROM audit_events WHERE event_type='operation.cost_refunded'").fetchone()[0]
        self.assertEqual(refunded,1)

    def test_reissue_requires_pending_decision(self):
        self._unresolved()
        with self.assertRaises(DomainError) as caught:
            self._reissue()
        self.assertEqual(caught.exception.code,'OPERATION_DECISION_NOT_PENDING')
        self.assertEqual(self.attempts(),1)

    def test_reissue_rejects_decision_of_other_attempt(self):
        self._unresolved()
        self.adapter.script('external.publish','unknown')
        self._dispatch({'attempt_id':'ATT-B','target_action':'external.publish',
                        'target_resource':'other','target_payload':{'n':0}},key='d-b')
        self._reconcile(attempt_id='ATT-B',key='rec-b')
        payload={'attempt_id':'ATT-B','decision_id':'DEC-1','new_attempt_id':'ATT-C',
                 'target_action':'external.publish','target_resource':'other',
                 'target_payload':{'n':0}}
        with self.s.tx():
            info=self.r.enforce(self.actor,'operation.reissue',payload)
            with self.assertRaises(DomainError) as caught:
                self.manager.reissue(self.actor,payload,info,'rei-mismatch')
        self.assertEqual(caught.exception.code,'OPERATION_DECISION_NOT_PENDING')

    def test_reissue_rejects_consumed_decision(self):
        self._unresolved()
        self._decide()
        self.adapter.script('external.publish','succeeded')
        self._reissue()
        with self.assertRaises(DomainError) as caught:
            self._reissue(key='rei-again')
        self.assertEqual(caught.exception.code,'OPERATION_DECISION_NOT_PENDING')
        self.assertEqual(self.attempts(),2)

    def test_reissue_rejects_expired_decision(self):
        self._unresolved()
        self._insert_decision(decision_id='DEC-LATE',expires=(NOW-timedelta(minutes=1)).isoformat())
        self.adapter.script('external.publish','succeeded')
        payload={'attempt_id':'ATT-1','decision_id':'DEC-LATE','new_attempt_id':'ATT-2',
                 'target_action':'external.publish','target_resource':'external:publish',
                 'target_payload':{'note':'x'}}
        with self.s.tx():
            info=self.r.enforce(self.actor,'operation.reissue',payload)
            with self.assertRaises(DomainError) as caught:
                self.manager.reissue(self.actor,payload,info,'rei-late')
        self.assertEqual(caught.exception.code,'OPERATION_DECISION_EXPIRED')
        pending=self.s.db.execute("SELECT consumed_by_attempt_id FROM operation_reconciliations WHERE id='DEC-LATE'").fetchone()['consumed_by_attempt_id']
        self.assertIsNone(pending)

    def test_reissue_rejects_guard_key_mismatch(self):
        self._unresolved()
        self._decide()
        payload={'attempt_id':'ATT-1','decision_id':'DEC-1','new_attempt_id':'ATT-2',
                 'target_action':'external.publish','target_resource':'external:publish',
                 'target_payload':{'note':'changed'}}
        with self.s.tx():
            info=self.r.enforce(self.actor,'operation.reissue',payload)
            with self.assertRaises(DomainError) as caught:
                self.manager.reissue(self.actor,payload,info,'rei-diff')
        self.assertEqual(caught.exception.code,'OPERATION_REISSUE_GUARD_MISMATCH')
        pending=self.s.db.execute("SELECT consumed_by_attempt_id FROM operation_reconciliations WHERE id='DEC-1'").fetchone()['consumed_by_attempt_id']
        self.assertIsNone(pending)
        self.assertEqual(self.attempts(),1)

    def test_reissue_rejects_non_unresolved_attempt(self):
        self.adapter.script('external.publish','succeeded')
        self._dispatch(self.payload,key='d-ok')
        self._insert_decision(decision_id='DEC-S',attempt_id='ATT-1')
        payload={'attempt_id':'ATT-1','decision_id':'DEC-S','new_attempt_id':'ATT-2',
                 'target_action':'external.publish','target_resource':'external:publish',
                 'target_payload':{'note':'x'}}
        with self.s.tx():
            info=self.r.enforce(self.actor,'operation.reissue',payload)
            with self.assertRaises(DomainError) as caught:
                self.manager.reissue(self.actor,payload,info,'rei-s')
        self.assertEqual(caught.exception.code,'OPERATION_REISSUE_NOT_ALLOWED')

    def test_abandon_keeps_guard_blocked(self):
        self._unresolved()
        self._decide(decision='abandon',decision_id='DEC-A')
        with self.assertRaises(DomainError) as caught:
            self._dispatch({'attempt_id':'ATT-2','target_action':'external.publish',
                            'target_resource':'external:publish','target_payload':{'note':'x'}},
                           key='retry')
        self.assertEqual(caught.exception.code,'OPERATION_UNRESOLVED_ATTEMPT_EXISTS')
        self.assertEqual(self.attempts(),1)

    def test_consumed_reissue_releases_guard_only_via_new_attempt(self):
        # After a consumed reissue decision the OLD attempt no longer blocks
        # its guard key, but the NEW attempt re-blocks it while unresolved.
        self._unresolved()
        self._decide()
        self.adapter.script('external.publish','unknown')
        result=self._reissue()
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(self.state_of('ATT-1'),'unresolved')
        with self.assertRaises(DomainError) as caught:
            self._dispatch({'attempt_id':'ATT-3','target_action':'external.publish',
                            'target_resource':'external:publish','target_payload':{'note':'x'}},
                           key='retry-2')
        self.assertEqual(caught.exception.code,'OPERATION_UNRESOLVED_ATTEMPT_EXISTS')
        # The new attempt enters the same human-gated cycle: reconcile to
        # unresolved, decide again; the provenance chain stays append-only.
        self._reconcile(attempt_id='ATT-2',key='rec-2')
        self._decide(decision_id='DEC-2',attempt_id='ATT-2')
        self.adapter.script('external.publish','succeeded')
        result=self._reissue(decision_id='DEC-2',new_attempt_id='ATT-4',key='rei-2',
                             attempt_id='ATT-2')
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(self.attempts(),3)
        self.assertEqual(self.state_of('ATT-2'),'unresolved')
        self.assertEqual(self.state_of('ATT-4'),'succeeded')

    def test_decision_provenance_is_immutable(self):
        self._unresolved()
        self._decide()
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute("UPDATE operation_reconciliations SET decision='abandon' WHERE id='DEC-1'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute("UPDATE operation_reconciliations SET attempt_id='ATT-9' WHERE id='DEC-1'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute("DELETE FROM operation_reconciliations WHERE id='DEC-1'")
        self.adapter.script('external.publish','succeeded')
        self._reissue()
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute("UPDATE operation_reconciliations SET consumed_by_attempt_id='ATT-9' WHERE id='DEC-1'")
        row=self.s.db.execute("SELECT consumed_by_attempt_id FROM operation_reconciliations WHERE id='DEC-1'").fetchone()
        self.assertEqual(row['consumed_by_attempt_id'],'ATT-2')

class TwoPhaseRecoveryTests(unittest.TestCase):
    """M5.5 T-PR1: commit-then-dispatch crash windows and the recovery command."""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Store(Path(self.tmp.name)/'twophase.db',fixture_bootstrap=True)
        d=self.s.db
        d.execute('INSERT INTO companies(id,state) VALUES (?,?)',('C','active'))
        d.execute('INSERT INTO positions VALUES (?,?,?,?)',('C','P','role.engineer@1',None))
        d.execute('INSERT INTO agents VALUES (?,?,?)',('C','AGT','permanent'))
        d.execute('INSERT INTO appointments VALUES (?,?,?,?,?)',('C','APT','AGT','P','active'))
        self.r=SQLitePolicyResolver(self.s)
        d.execute('INSERT INTO appointment_capabilities VALUES (?,?,?)',('C','APT','cap.dispatch'))
        d.execute('INSERT INTO company_policy_versions VALUES (?,?,?)',('C','pol@1',1))
        base={'eligible_roles':['role.engineer@1'],'required_capabilities':['cap.dispatch'],
              'allowed_task_states':[],'allowed_company_states':['active'],
              'task_required':False,'approval_required':False,'risk_level':'L2'}
        for action,cost in (('operation.dispatch',3),('operation.reconcile',0),
                            ('operation.decide',0),('operation.reissue',3),
                            ('operation.recover',0)):
            d.execute('INSERT INTO company_policy_rules VALUES (?,?,?,?,?)',
                      ('C','pol@1',action,json.dumps({**base,'action':action}),cost))
        for gid,action,resource in (('G-EXT','operation.dispatch','external:publish'),
                                    ('G-REC','operation.reconcile','company:C'),
                                    ('G-DEC','operation.decide','company:C'),
                                    ('G-REI','operation.reissue','external:publish'),
                                    ('G-RCV','operation.recover','company:C')):
            d.execute('INSERT INTO policy_grants VALUES (?,?,?,?,?,?,?,?,?)',
                      ('C',gid,'APT',None,action,resource,
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

    def _authorize(self,payload,key='k1'):
        """Phase 1 only: authorization commits, effect never runs."""
        with self.s.tx():
            info=self.r.enforce(self.actor,'operation.dispatch',payload)
            self.manager.authorize_dispatch(self.actor,payload,info,key)
        return info

    def _dispatch(self,payload,key='k1'):
        with self.s.tx():
            info=self.r.enforce(self.actor,'operation.dispatch',payload)
            self.manager.authorize_dispatch(self.actor,payload,info,key)
        call=self.manager.execute_effect(self.actor,payload,info)
        with self.s.tx():
            return self.manager.settle_dispatch(self.actor,payload,call,key)

    def _recover(self,attempt_id='ATT-1',key='rec-x'):
        with self.s.tx():
            return self.manager.recover(self.actor,{'attempt_id':attempt_id},key)

    def _reconcile(self,attempt_id='ATT-1',key='rec-1'):
        with self.s.tx():
            return self.manager.reconcile(self.actor,{'attempt_id':attempt_id},key)

    def budget(self):
        return self.s.db.execute(
            "SELECT remaining_units FROM policy_budgets WHERE company_id='C'").fetchone()['remaining_units']

    def state_of(self,attempt_id):
        return self.s.db.execute(
            '''SELECT state FROM operation_attempts WHERE company_id='C' AND id=?''',
            (attempt_id,)).fetchone()['state']

    def test_phase1_commits_with_transport_identity(self):
        self.adapter.script('external.publish','crash')
        with self.assertRaises(RuntimeError):
            self._dispatch(self.payload)
        row=self.s.db.execute('''SELECT state,transport_key,adapter_ref
            FROM operation_attempts WHERE company_id='C' AND id='ATT-1' ''').fetchone()
        self.assertEqual(row['state'],'authorized')
        self.assertEqual(row['transport_key'],'IDEM-ATT-1')
        self.assertIsNone(row['adapter_ref'])
        self.assertEqual(self.budget(),7)
        authorized=self.s.db.execute("SELECT count(*) FROM audit_events WHERE event_type='operation.authorized'").fetchone()[0]
        self.assertEqual(authorized,1)

    def test_transport_identity_is_immutable(self):
        self._authorize(self.payload)
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute("UPDATE operation_attempts SET transport_key='IDEM-FORGED' WHERE id='ATT-1'")

    def test_crash_after_send_recovers_to_succeeded(self):
        self.adapter.script('external.publish','crash')
        with self.assertRaises(RuntimeError):
            self._dispatch(self.payload)
        evidence=self.adapter.status_for_transport('IDEM-ATT-1')
        self.assertIsNotNone(evidence)
        self.adapter.script_status(evidence['adapter_ref'],'succeeded')
        result=self._recover()
        self.assertEqual(result,{'attempt_id':'ATT-1','state':'succeeded','recovered':True})
        self.assertEqual(self.state_of('ATT-1'),'succeeded')
        self.assertEqual(self.budget(),7)
        recovered=self.s.db.execute("SELECT count(*) FROM audit_events WHERE event_type='operation.recovered'").fetchone()[0]
        self.assertEqual(recovered,1)

    def test_crash_after_send_recovers_to_failed_refunds(self):
        self.adapter.script('external.publish','crash')
        with self.assertRaises(RuntimeError):
            self._dispatch(self.payload)
        evidence=self.adapter.status_for_transport('IDEM-ATT-1')
        self.adapter.script_status(evidence['adapter_ref'],'failed')
        result=self._recover()
        self.assertEqual(result['state'],'failed')
        self.assertEqual(self.budget(),10)

    def test_recover_without_evidence_maps_unknown_enters_human_path(self):
        # Window 1: the process died before the call ever went out.
        self._authorize(self.payload)
        result=self._recover()
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(self.budget(),7)
        # The existing human-gated machinery takes over unchanged (owner Q1).
        result=self._reconcile()
        self.assertEqual(result['state'],'unresolved')
        with self.assertRaises(DomainError) as caught:
            self._dispatch({'attempt_id':'ATT-2','target_action':'external.publish',
                            'target_resource':'external:publish','target_payload':{'note':'x'}},
                           key='retry')
        self.assertEqual(caught.exception.code,'OPERATION_UNRESOLVED_ATTEMPT_EXISTS')

    def test_recover_resolves_dispatched_rest_state(self):
        self.adapter.script('external.publish','crash')
        with self.assertRaises(RuntimeError):
            self._dispatch(self.payload)
        # Trusted fixture write modeling a ledger that learned `dispatched`
        # but never settled the outcome (possible with a real transport).
        self.s.db.execute("UPDATE operation_attempts SET state='dispatched' WHERE id='ATT-1'")
        evidence=self.adapter.status_for_transport('IDEM-ATT-1')
        self.adapter.script_status(evidence['adapter_ref'],'succeeded')
        result=self._recover()
        self.assertEqual(result['state'],'succeeded')

    def test_recover_requires_midflight_state(self):
        self.adapter.script('external.publish','succeeded')
        self._dispatch(self.payload)
        with self.assertRaises(DomainError) as caught:
            self._recover()
        self.assertEqual(caught.exception.code,'OPERATION_RECOVER_NOT_ALLOWED')
        self._recover_no_evidence_to_unknown()
        with self.assertRaises(DomainError) as caught:
            self._recover()
        self.assertEqual(caught.exception.code,'OPERATION_RECOVER_NOT_ALLOWED')

    def _recover_no_evidence_to_unknown(self):
        payload={'attempt_id':'ATT-9','target_action':'external.publish',
                 'target_resource':'external:publish','target_payload':{'n':0}}
        self._authorize(payload,key='k9')
        self._recover('ATT-9')

    def test_midflight_attempt_blocks_same_key_dispatch(self):
        self.adapter.script('external.publish','crash')
        with self.assertRaises(RuntimeError):
            self._dispatch(self.payload)
        with self.assertRaises(DomainError) as caught:
            self._dispatch({'attempt_id':'ATT-2','target_action':'external.publish',
                            'target_resource':'external:publish','target_payload':{'note':'x'}},
                           key='retry')
        self.assertEqual(caught.exception.code,'OPERATION_MIDFLIGHT_ATTEMPT_EXISTS')
        self.assertEqual(self.s.db.execute(
            'SELECT count(*) FROM operation_attempts').fetchone()[0],1)

    def test_reissue_effect_crash_then_recover(self):
        self.adapter.script('external.publish','unknown')
        self._dispatch(self.payload,key='d1')
        with self.s.tx():
            self.manager.reconcile(self.actor,{'attempt_id':'ATT-1'},'rec-1')
        with self.s.tx():
            di=self.r.enforce(self.actor,'operation.decide',
                {'attempt_id':'ATT-1','decision_id':'DEC-1','decision':'reissue',
                 'expires_at':(NOW+timedelta(hours=1)).isoformat()})
            self.manager.decide(self.actor,{'attempt_id':'ATT-1','decision_id':'DEC-1',
                'decision':'reissue','expires_at':(NOW+timedelta(hours=1)).isoformat()},di,'dec-1')
        reissue_payload={'attempt_id':'ATT-1','decision_id':'DEC-1',
                         'new_attempt_id':'ATT-2','target_action':'external.publish',
                         'target_resource':'external:publish','target_payload':{'note':'x'}}
        with self.s.tx():
            ri=self.r.enforce(self.actor,'operation.reissue',reissue_payload)
            self.manager.reissue(self.actor,reissue_payload,ri,'rei-1')
        # The re-dispatched effect crashes after the provider accepted it.
        phase2={'attempt_id':'ATT-2','target_action':'external.publish',
                'target_resource':'external:publish','target_payload':{'note':'x'}}
        with self.assertRaises(RuntimeError):
            self.manager.execute_effect(self.actor,phase2,ri)
        self.assertEqual(self.state_of('ATT-2'),'authorized')
        # Decision consumption stays committed; recovery resolves the orphan.
        result=self._recover('ATT-2')
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(self.state_of('ATT-1'),'unresolved')
        consumed=self.s.db.execute("SELECT consumed_by_attempt_id FROM operation_reconciliations WHERE id='DEC-1'").fetchone()['consumed_by_attempt_id']
        self.assertEqual(consumed,'ATT-2')

if __name__ == '__main__':
    unittest.main()
