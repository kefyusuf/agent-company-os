"""M5.3-A: trusted LOCAL fixture session, never an internet auth boundary."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from company_os import DomainError, Store
from company_os.gateway import CommandGateway, FixtureSessionHost


class CommandGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'gateway.db'
        self.store = Store(self.path, fixture_bootstrap=True)
        self.store.bootstrap('COMPANY-A', [
            ('POS-CEO', 'core.ceo@1', None, 'AGT-CEO', 'APT-CEO'),
            ('POS-CTO', 'software.cto@1', 'POS-CEO', 'AGT-CTO', 'APT-CTO'),
            ('POS-BACK', 'software.backend@1', 'POS-CTO', 'AGT-BACK', 'APT-BACK'),
        ])
        with self.store.tx():
            self.store.db.execute("UPDATE companies SET state='active' WHERE id='COMPANY-A'")
            self.store.audit('COMPANY-A','company','COMPANY-A',
                             'fixture.simulated_activation','system:fixture',{})
        self.host = FixtureSessionHost()
        self.ceo = self.host.issue('COMPANY-A','AGT-CEO')
        self.cto = self.host.issue('COMPANY-A','AGT-CTO')
        self.backend = self.host.issue('COMPANY-A','AGT-BACK')
        self.gateway = CommandGateway(self.store, self.host)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def cmd(self, typ='task.create', payload=None, *, company='COMPANY-A',
            appointment='APT-CEO', key='cmd1'):
        if payload is None:
            payload={'task_id':'ROOT','objective':'Investigate safe local fixture'}
        return {'type':typ,'company_id':company,'appointment_id':appointment,
                'key':key,'payload':payload}

    def denies(self, code, func):
        with self.assertRaises(DomainError) as error:
            func()
        self.assertEqual(error.exception.code,code)

    def task_count(self):
        return self.store.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]

    def denied_events(self):
        return self.store.db.execute('''SELECT COUNT(*) FROM audit_events
            WHERE company_id=? AND event_type=?''',('COMPANY-A','command.denied')).fetchone()[0]

    def root(self):
        self.gateway.execute(self.ceo,self.cmd())
        self.gateway.execute(self.ceo,self.cmd('task.transition',
            {'task_id':'ROOT','target':'ready','expected_revision':1},key='ready'))
        self.gateway.execute(self.ceo,self.cmd('task.transition',
            {'task_id':'ROOT','target':'assigned','expected_revision':2},key='assigned'))
        self.gateway.execute(self.ceo,self.cmd('task.start',
            {'task_id':'ROOT','expected_revision':3},key='start'))

    def delegate(self):
        self.root()
        offer=self.gateway.execute(self.ceo,self.cmd('delegation.offer',
            {'parent_task_id':'ROOT','offer_id':'DEL-CTO','child_task_id':'TASK-CTO',
             'target_appointment_id':'APT-CTO','objective':'Inspect local example',
             'expected_revision':4},key='offer'))
        self.assertEqual(offer['state'],'offered')
        accepted=self.gateway.execute(self.cto,self.cmd('delegation.accept',
            {'offer_id':'DEL-CTO','expected_revision':1},
            appointment='APT-CTO',key='accept-offer'))
        self.assertEqual(accepted['state'],'accepted')
        return self.gateway.execute(self.cto,self.cmd('task.start',
            {'task_id':'TASK-CTO','expected_revision':1},
            appointment='APT-CTO',key='start-child'))

    def test_valid_root_create_and_replay_does_not_duplicate(self):
        one=self.gateway.execute(self.ceo,self.cmd())
        self.assertEqual(one,self.gateway.execute(self.ceo,self.cmd()))
        self.assertEqual(self.task_count(),1)
        self.assertEqual(self.denied_events(),0)

    def test_untrusted_dictionary_session_denied(self):
        self.denies('AUTH_UNTRUSTED_SESSION',lambda:self.gateway.execute(
            {'company_id':'COMPANY-A','agent_id':'AGT-CEO'},self.cmd()))
        self.assertEqual(self.task_count(),0)

    def test_lookalike_session_rejected(self):
        fake=type(self.ceo)()
        self.denies('AUTH_UNTRUSTED_SESSION',lambda:self.gateway.execute(fake,self.cmd()))

    def test_revoked_session_denied(self):
        self.host.revoke(self.ceo)
        self.denies('AUTH_UNTRUSTED_SESSION',lambda:self.gateway.execute(self.ceo,self.cmd()))

    def test_actor_claim_in_command_rejected_and_logged(self):
        proposal=self.cmd()
        proposal['actor']={'agent_id':'AGT-CTO','appointment_id':'APT-CTO'}
        self.denies('SCHEMA_INVALID_COMMAND',lambda:self.gateway.execute(self.ceo,proposal))
        self.assertEqual(self.task_count(),0)
        self.assertEqual(self.denied_events(),1)

    def test_actor_claim_in_payload_rejected(self):
        proposal=self.cmd()
        proposal['payload']['agent_id']='AGT-CTO'
        self.denies('SCHEMA_INVALID_PAYLOAD',lambda:self.gateway.execute(self.ceo,proposal))

    def test_cannot_cross_tenant_even_with_attested_principal(self):
        self.store.bootstrap('COMPANY-B', [('POS-B-CEO','core.ceo@1',None,
                                            'AGT-B-CEO','APT-B-CEO')])
        self.denies('AUTH_TENANT_MISMATCH',lambda:self.gateway.execute(
            self.ceo,self.cmd(company='COMPANY-B')))
        self.assertEqual(self.task_count(),0)
        other_log=self.store.db.execute('''SELECT COUNT(*) FROM audit_events WHERE
            company_id='COMPANY-B' AND event_type='command.denied' ''').fetchone()[0]
        self.assertEqual(other_log,0)
        self.assertEqual(self.denied_events(),1)

    def test_session_cannot_select_other_agents_appointment(self):
        self.denies('AUTH_INVALID_APPOINTMENT',lambda:self.gateway.execute(
            self.ceo,self.cmd(appointment='APT-CTO')))
        self.assertEqual(self.task_count(),0)

    def test_ended_appointment_invalidates_existing_session(self):
        with self.store.tx():
            self.store.db.execute("UPDATE appointments SET status='ended' WHERE id='APT-CEO'")
        self.denies('AUTH_INVALID_APPOINTMENT',lambda:self.gateway.execute(self.ceo,self.cmd()))

    def test_unknown_action_default_denied(self):
        self.denies('AUTH_UNKNOWN_ACTION',lambda:self.gateway.execute(
            self.ceo,self.cmd('production.deploy',{},key='deploy')))
        self.assertEqual(self.task_count(),0)
        self.assertEqual(self.denied_events(),1)

    def test_non_ceo_cannot_create_root_task(self):
        self.denies('AUTH_ROOT_TASK_REQUIRES_CEO',lambda:self.gateway.execute(
            self.backend,self.cmd(appointment='APT-BACK')))
        self.assertEqual(self.task_count(),0)

    def test_company_pause_denies_new_commands(self):
        with self.store.tx():
            self.store.db.execute("UPDATE companies SET state='paused' WHERE id='COMPANY-A'")
        self.denies('AUTH_COMPANY_NOT_ACTIVE',lambda:self.gateway.execute(self.ceo,self.cmd()))
        self.assertEqual(self.task_count(),0)

    def test_stale_revision_and_deny_audit_does_not_mutate_work(self):
        self.gateway.execute(self.ceo,self.cmd())
        self.denies('STALE_REVISION',lambda:self.gateway.execute(self.ceo,self.cmd(
            'task.transition',{'task_id':'ROOT','target':'ready','expected_revision':99},key='bad-rev')))
        record=self.store.db.execute('SELECT state,revision FROM tasks WHERE id=?',('ROOT',)).fetchone()
        self.assertEqual((record['state'],record['revision']),('draft',1))
        self.assertEqual(self.denied_events(),1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM command_receipts').fetchone()[0],1)

    def test_idempotency_same_key_changed_payload_denied(self):
        self.gateway.execute(self.ceo,self.cmd())
        self.denies('IDEMPOTENCY_CONFLICT',lambda:self.gateway.execute(self.ceo,self.cmd(
            payload={'task_id':'OTHER','objective':'Changed'},key='cmd1')))
        self.assertEqual(self.task_count(),1)
        self.assertEqual(self.denied_events(),1)

    def test_cross_appointment_direct_assignment_not_exposed(self):
        self.denies('SCHEMA_INVALID_PAYLOAD',lambda:self.gateway.execute(self.ceo,self.cmd(
            payload={'task_id':'CHILD','objective':'Bypass',
                     'accountable_appointment_id':'APT-CTO'})))

    def test_direct_delegation_to_non_direct_report_denied(self):
        self.root()
        self.denies('AUTH_NOT_DIRECT_REPORT',lambda:self.gateway.execute(self.ceo,self.cmd(
            'delegation.offer',{'parent_task_id':'ROOT','offer_id':'DEL-BAD',
                                'child_task_id':'CHILD','target_appointment_id':'APT-BACK',
                                'objective':'Illegal skip','expected_revision':4},key='bad-offer')))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM delegations').fetchone()[0],0)

    def test_valid_delegation_report_review_and_acceptance(self):
        self.delegate()
        sha=hashlib.sha256(b'fixture evidence').hexdigest()
        self.gateway.execute(self.cto,self.cmd('evidence.add',
            {'task_id':'TASK-CTO','evidence_id':'E-1',
             'source_ref':'fixture://local','content_sha256':sha},
            appointment='APT-CTO',key='evidence'))
        submitted=self.gateway.execute(self.cto,self.cmd('report.submit',
            {'task_id':'TASK-CTO','report_id':'R-1','version':1,
             'content':'Evidence-backed local report','expected_revision':2,
             'evidence_ids':['E-1'],'source_reports':[]},
            appointment='APT-CTO',key='report'))
        self.assertEqual(submitted['state'],'submitted')
        self.gateway.execute(self.ceo,self.cmd('report.begin_review',
            {'report_id':'R-1','version':1},key='review-start'))
        reviewed=self.gateway.execute(self.ceo,self.cmd('report.review',
            {'report_id':'R-1','version':1,'review_id':'REV-1',
             'decision':'accepted','reason':'Source reference exists'},key='review-end'))
        self.assertEqual(reviewed['task_state'],'under_review')
        result=self.gateway.execute(self.ceo,self.cmd('task.accept',
            {'task_id':'TASK-CTO','expected_revision':5},key='task-accept'))
        self.assertEqual(result['state'],'accepted')
        sources=self.gateway.execute(self.ceo,self.cmd('report.sources',
            {'report_id':'R-1','version':1},key='sources'))
        self.assertEqual(sources['evidence_ids'],['E-1'])

    def test_foreign_peer_cannot_read_report_sources(self):
        self.delegate()
        sha=hashlib.sha256(b'e').hexdigest()
        self.gateway.execute(self.cto,self.cmd('evidence.add',
            {'task_id':'TASK-CTO','evidence_id':'EV','source_ref':'fixture://ev',
             'content_sha256':sha},appointment='APT-CTO',key='evidence'))
        self.gateway.execute(self.cto,self.cmd('report.submit',
            {'task_id':'TASK-CTO','report_id':'REP','version':1,'content':'Safe',
             'expected_revision':2,'evidence_ids':['EV']},
            appointment='APT-CTO',key='submit'))
        self.denies('AUTH_REPORT_SCOPE',lambda:self.gateway.execute(self.backend,
            self.cmd('report.sources',{'report_id':'REP','version':1},
                     appointment='APT-BACK',key='read')))

    def test_review_not_independent_across_two_appointments(self):
        self.delegate()
        sha=hashlib.sha256(b'e').hexdigest()
        self.gateway.execute(self.cto,self.cmd('evidence.add',
            {'task_id':'TASK-CTO','evidence_id':'EV',
             'source_ref':'fixture://ev','content_sha256':sha},
            appointment='APT-CTO',key='ev'))
        self.gateway.execute(self.cto,self.cmd('report.submit',
            {'task_id':'TASK-CTO','report_id':'REP','version':1,'content':'Test',
             'expected_revision':2,'evidence_ids':['EV']},
            appointment='APT-CTO',key='submit'))
        with self.store.tx():
            self.store.db.execute("UPDATE appointments SET agent_id='AGT-CTO' WHERE id='APT-CEO'")
        shared=self.host.issue('COMPANY-A','AGT-CTO')
        self.denies('CONFLICT_SAME_AGENT_IDENTITY',lambda:self.gateway.execute(
            shared,self.cmd('report.begin_review',{'report_id':'REP','version':1},key='self-review')))

    def test_replay_is_denied_after_session_revocation(self):
        self.gateway.execute(self.ceo,self.cmd())
        self.host.revoke(self.ceo)
        self.denies('AUTH_UNTRUSTED_SESSION',lambda:self.gateway.execute(self.ceo,self.cmd()))

    def test_privilege_claim_in_unknown_payload_never_logged(self):
        proposal=self.cmd('policy.write',{'secret':'DO-NOT-LOG'},key='attacker')
        self.denies('AUTH_UNKNOWN_ACTION',lambda:self.gateway.execute(self.ceo,proposal))
        events=self.store.db.execute("SELECT payload FROM audit_events WHERE event_type='command.denied'").fetchall()
        self.assertEqual(len(events),1)
        self.assertNotIn('DO-NOT-LOG',events[0]['payload'])

    def test_nested_transaction_rollback_of_gateway_action_and_receipt(self):
        with self.assertRaisesRegex(RuntimeError,'outer rollback'):
            with self.store.tx():
                self.gateway.execute(self.ceo,self.cmd())
                raise RuntimeError('outer rollback')
        self.assertEqual(self.task_count(),0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM command_receipts').fetchone()[0],0)

    def test_boolean_revision_rejected_by_schema(self):
        self.denies('SCHEMA_INVALID_PAYLOAD',lambda:self.gateway.execute(self.ceo,self.cmd(
            'task.transition',{'task_id':'ROOT','target':'ready','expected_revision':True},key='bool')))

    def test_read_only_sources_during_pause_with_existing_task(self):
        self.delegate()
        sha=hashlib.sha256(b'e').hexdigest()
        self.gateway.execute(self.cto,self.cmd('evidence.add',
            {'task_id':'TASK-CTO','evidence_id':'EV','source_ref':'fixture://ev',
             'content_sha256':sha},appointment='APT-CTO',key='evid'))
        self.gateway.execute(self.cto,self.cmd('report.submit',
            {'task_id':'TASK-CTO','report_id':'REP','version':1,'content':'Safe',
             'expected_revision':2,'evidence_ids':['EV']},appointment='APT-CTO',key='rep'))
        with self.store.tx():
            self.store.db.execute("UPDATE companies SET state='paused' WHERE id='COMPANY-A'")
        r=self.gateway.execute(self.ceo,self.cmd('report.sources',
            {'report_id':'REP','version':1},key='read'))
        self.assertEqual(r['evidence_ids'],['EV'])


if __name__ == '__main__':
    unittest.main()