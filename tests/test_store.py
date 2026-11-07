import tempfile
import unittest
from pathlib import Path

from company_os import Actor, Store, DomainError
from company_os.fixtures import seed_demo


class ContractFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'demo.db'
        self.store = Store(self.path, fixture_bootstrap=True)
        self.ceo = seed_demo(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def create(self, **kw):
        args = {'actor':self.ceo, 'task_id':'TASK-1', 'objective':'Research API latency',
                'accountable':'APPOINT-CEO', 'assigned':'APPOINT-CEO', 'key':'create-1'}
        return self.store.create_task(**(args|kw))

    def denies(self, code, action):
        with self.assertRaises(DomainError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_fixture_stays_inactive_and_has_four_appointments(self):
        self.assertEqual(self.store.db.execute('SELECT state FROM companies').fetchone()[0], 'inactive')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM appointments').fetchone()[0], 4)

    def test_task_survives_restart(self):
        self.create()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.task(self.ceo,'TASK-1')['state'],'draft')
        self.assertEqual(len(self.store.events(self.ceo)), 6)

    def test_fixture_bootstrap_disabled_by_default(self):
        self.store.close()
        self.store = Store(self.path)
        self.denies('BOOTSTRAP_DISABLED', lambda:seed_demo(self.store,'OTHER'))

    def test_separate_tenant_cannot_read_other_company_task(self):
        self.create()
        other = seed_demo(self.store, 'COMPANY-OTHER')
        self.denies('REFERENCE_TASK_NOT_FOUND', lambda:self.store.task(other, 'TASK-1'))
        self.assertEqual(len(self.store.events(other)), 5)

    def test_identity_appointment_mismatch_denied(self):
        self.denies('AUTH_INVALID_APPOINTMENT',lambda:self.create(
            actor=Actor(self.ceo.company_id,'AGT-BACKEND','APPOINT-CEO')))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM tasks').fetchone()[0],0)

    def test_invalid_accountable_denied(self):
        self.denies('AUTH_INVALID_TASK_APPOINTMENT',lambda:self.create(accountable='APPOINT-UNKNOWN'))

    def test_cross_appointment_work_requires_delegation(self):
        self.denies('AUTH_DELEGATION_REQUIRED', lambda:self.create(
            accountable='APPOINT-CTO', assigned='APPOINT-CTO'))
        self.denies('AUTH_DELEGATION_REQUIRED', lambda:self.create(assigned='APPOINT-CTO'))

    def test_invalid_parent_denied(self):
        self.denies('REFERENCE_PARENT_NOT_FOUND',lambda:self.create(parent_id='NOT-FOUND'))

    def test_create_idempotent_single_audit(self):
        before=len(self.store.events(self.ceo))
        self.assertEqual(self.create(),self.create())
        self.assertEqual(len(self.store.events(self.ceo)),before+1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM tasks').fetchone()[0],1)

    def test_same_key_different_payload_conflicts(self):
        self.create()
        self.denies('IDEMPOTENCY_CONFLICT',lambda:self.create(objective='Other objective'))

    def test_planning_state_transitions(self):
        self.create()
        ready=self.store.transition_task(self.ceo,'TASK-1','ready',1,'ready-1')
        self.assertEqual(ready['revision'],2)
        assigned=self.store.transition_task(self.ceo,'TASK-1','assigned',2,'assigned-1')
        self.assertEqual(assigned['state'],'assigned')

    def test_transition_replay_after_revision_change(self):
        self.create()
        one=self.store.transition_task(self.ceo,'TASK-1','ready',1,'ready-1')
        self.assertEqual(one,self.store.transition_task(self.ceo,'TASK-1','ready',1,'ready-1'))
        self.assertEqual(self.store.task(self.ceo,'TASK-1')['revision'],2)

    def test_stale_revision_rejected_without_audit(self):
        self.create()
        count=len(self.store.events(self.ceo))
        self.denies('STALE_REVISION',lambda:self.store.transition_task(self.ceo,'TASK-1','ready',99,'old'))
        self.assertEqual(len(self.store.events(self.ceo)),count)

    def test_no_completion_shortcut(self):
        self.create()
        count=len(self.store.events(self.ceo))
        for target in ('accepted','closed','in_progress'):
            self.denies('STATE_TRANSITION_NOT_IMPLEMENTED_OR_FORBIDDEN',
                        lambda t=target:self.store.transition_task(self.ceo,'TASK-1',t,1,t+'-1'))
        self.assertEqual(len(self.store.events(self.ceo)),count)

    def test_only_accountable_appointment_may_transition(self):
        self.create()
        self.denies('AUTH_NOT_ACCOUNTABLE',lambda:self.store.transition_task(
            Actor(self.ceo.company_id,'AGT-CTO','APPOINT-CTO'),'TASK-1','ready',1,'other'))

    def test_self_or_unconnected_manager_denied_on_fixture(self):
        self.store.close()
        self.store = Store(self.path,fixture_bootstrap=True)
        self.denies('ORG_INVALID_ROOT',lambda:self.store.bootstrap('COMPANY-OTHER',[
            ('POS-INVALID','software.cto@1',None,'AGT-1','APT-1')]))
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM companies WHERE id=?',('COMPANY-OTHER',)).fetchone())


if __name__=='__main__':
    unittest.main()
