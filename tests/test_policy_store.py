"""Policy resolver tests against the repository Store and isolated SQLite state."""
import hashlib
import json
import unittest
import tempfile
from pathlib import Path
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from company_os.store import Store,Actor,DomainError,canonical
from company_os.policy_store import SQLitePolicyResolver
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

    def test_positive_cost_not_executable(self):
        self.set_rule(self.rule,cost=1)
        self.rejected('BUDGET_RESERVATION_NOT_IMPLEMENTED')

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

    def test_approved_snapshot_still_no_execution(self):
        rule={**self.rule,'approval_required':True,'risk_level':'L3'}
        self.set_rule(rule)
        intent='sha256:'+hashlib.sha256(canonical({
            'company_id':'C','action':self.action,'task_id':'T',
            'resource':'task:T','payload':self.payload}).encode()).hexdigest()
        self.s.db.execute('INSERT INTO policy_approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
          ('C','APR','T',self.action,'task:T',intent,1,'pol@1',
           (NOW+timedelta(minutes=5)).isoformat(),'approved','human','OWNER'))
        self.rejected('AUTH_APPROVAL_CONSUMPTION_NOT_IMPLEMENTED')

    def test_approval_wrong_payload(self):
        rule={**self.rule,'approval_required':True,'risk_level':'L3'}
        self.set_rule(rule)
        self.s.db.execute('INSERT INTO policy_approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
          ('C','APR','T',self.action,'task:T','sha256:'+'0'*64,1,'pol@1',
           (NOW+timedelta(minutes=5)).isoformat(),'approved','human','OWNER'))
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

if __name__=='__main__': unittest.main()
