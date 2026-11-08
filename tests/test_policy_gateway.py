"""M5.3-B2: integration tests with the repository's real Store/CommandGateway.

These tests require the M5.3-A Store, Workflows and FixtureSessionHost.
Run against the complete GitHub branch to verify gateway integration.
"""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from company_os import DomainError, Store
from company_os.gateway import FixtureSessionHost, PolicyCommandGateway
from company_os.policy_store import SQLitePolicyResolver
from company_os.store import canonical


class PolicyGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'policy-gateway.db',fixture_bootstrap=True)
        self.store.bootstrap('C-A',[
            ('POS-CEO','core.ceo@1',None,'AGT-CEO','APT-CEO'),
            ('POS-CTO','software.cto@1','POS-CEO','AGT-CTO','APT-CTO'),
        ])
        with self.store.tx():
            self.store.db.execute('UPDATE companies SET state=? WHERE id=?',('active','C-A'))
        self.host=FixtureSessionHost()
        self.ceo=self.host.issue('C-A','AGT-CEO')
        self.cto=self.host.issue('C-A','AGT-CTO')
        self.resolver=SQLitePolicyResolver(self.store)
        self.gateway=PolicyCommandGateway(self.store,self.host,self.resolver)
        self.version='test-policy@1'
        self.store.db.execute('INSERT INTO company_policy_versions VALUES (?,?,?)',('C-A',self.version,1))
        self.set_rule('task.create',role='core.ceo@1',task_required=False,states=[],capabilities=['cap.planning'])
        self.store.db.execute('INSERT INTO appointment_capabilities VALUES (?,?,?)',('C-A','APT-CEO','cap.planning'))
        self.grant('G-ROOT','APT-CEO',None,'task.create','company:C-A')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def cmd(self,action='task.create',payload=None,appointment='APT-CEO',key='c1',company='C-A'):
        if payload is None:
            payload={'task_id':'ROOT','objective':'Policy-protected deterministic task'}
        return {'type':action,'company_id':company,'appointment_id':appointment,
                'key':key,'payload':payload}

    def set_rule(self,action,role='core.ceo@1',task_required=True,states=None,
                 capabilities=None,approval=False,risk='L1'):
        if states is None:
            states=['draft']
        value={'action':action,'eligible_roles':[role],
               'allowed_task_states':states,
               'allowed_company_states':['active'],
               'task_required':task_required,
               'required_capabilities':capabilities or [],
               'approval_required':approval,'risk_level':risk}
        self.store.db.execute('INSERT OR REPLACE INTO company_policy_rules VALUES (?,?,?,?,?)',
                              ('C-A',self.version,action,json.dumps(value),0))

    def grant(self,gid,appointment,task_id,action,resource,expires=True):
        valid_until=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat() if expires else None
        self.store.db.execute('INSERT INTO policy_grants VALUES (?,?,?,?,?,?,?,?,?)',
                              ('C-A',gid,appointment,task_id,action,resource,valid_until,0,1))

    def assert_denied(self,code,fun):
        with self.assertRaises(DomainError) as caught:
            fun()
        self.assertEqual(caught.exception.code,code)

    def state(self):
        item=self.store.db.execute('SELECT state,revision FROM tasks WHERE company_id=? AND id=?',
                                   ('C-A','ROOT')).fetchone()
        return tuple(item) if item else None

    def create_root(self):
        return self.gateway.execute(self.ceo,self.cmd())

    def enable_ready(self):
        self.set_rule('task.transition',states=['draft','ready'])
        self.grant('G-READY','APT-CEO','ROOT','task.transition','task:ROOT')

    def test_permitted_create_is_audited_and_receipted(self):
        result=self.create_root()
        self.assertEqual(result['state'],'draft')
        self.assertEqual(self.state(),('draft',1))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM command_receipts').fetchone()[0],1)

    def test_policy_version_audited_with_successful_command(self):
        self.create_root()
        rows=self.store.db.execute('''SELECT payload FROM audit_events
            WHERE event_type=? AND aggregate_id=?''',
            ('authorization.permitted','task.create')).fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual(json.loads(rows[0]['payload'])['policy_version'],self.version)

    def test_downstream_rejection_rolls_back_policy_permit_event(self):
        self.create_root()
        self.set_rule('task.transition',states=['draft'])
        self.grant('G-READY','APT-CEO','ROOT','task.transition','task:ROOT')
        self.assert_denied('STATE_TRANSITION_NOT_IMPLEMENTED_OR_FORBIDDEN',
            lambda:self.gateway.execute(self.ceo,self.cmd('task.transition',
              {'task_id':'ROOT','target':'accepted','expected_revision':1},key='illegal')))
        permits=self.store.db.execute('''SELECT count(*) FROM audit_events
            WHERE event_type=? AND aggregate_id=?''',
            ('authorization.permitted','task.transition')).fetchone()[0]
        self.assertEqual(permits,0)
        self.assertEqual(self.state(),('draft',1))
        self.assertEqual(self.store.db.execute(
            "SELECT count(*) FROM policy_gateway_receipts WHERE idempotency_key='illegal'").fetchone()[0],0)

    def test_create_idempotent(self):
        self.assertEqual(self.create_root(),self.create_root())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM tasks').fetchone()[0],1)

    def test_replayed_transition_returns_original_result(self):
        self.create_root()
        self.enable_ready()
        cmd=self.cmd('task.transition',
            {'task_id':'ROOT','target':'ready','expected_revision':1},key='ready-once')
        first=self.gateway.execute(self.ceo,cmd)
        self.assertEqual(first['revision'],2)
        audit_before=self.store.db.execute('''SELECT count(*) FROM audit_events
            WHERE event_type='task.ready' ''').fetchone()[0]
        self.assertEqual(self.gateway.execute(self.ceo,cmd),first)
        self.assertEqual(self.state(),('ready',2))
        self.assertEqual(self.store.db.execute('''SELECT count(*) FROM audit_events
            WHERE event_type='task.ready' ''').fetchone()[0],audit_before)
        self.assertEqual(self.store.db.execute('''SELECT count(*) FROM
            policy_gateway_receipts WHERE idempotency_key='ready-once' ''').fetchone()[0],1)

    def test_changed_replay_payload_conflicts(self):
        self.create_root()
        self.enable_ready()
        self.gateway.execute(self.ceo,self.cmd('task.transition',
            {'task_id':'ROOT','target':'ready','expected_revision':1},key='mutate'))
        self.assert_denied('IDEMPOTENCY_CONFLICT',lambda:self.gateway.execute(
            self.ceo,self.cmd('task.transition',
            {'task_id':'ROOT','target':'ready','expected_revision':2},key='mutate')))
        self.assertEqual(self.state(),('ready',2))

    def test_replay_rejects_revoked_grant(self):
        self.create_root()
        self.enable_ready()
        command=self.cmd('task.transition',
            {'task_id':'ROOT','target':'ready','expected_revision':1},key='revoke')
        self.gateway.execute(self.ceo,command)
        self.store.db.execute("UPDATE policy_grants SET active=0 WHERE id='G-READY'")
        self.assert_denied('AUTH_GRANT_SCOPE',
            lambda:self.gateway.execute(self.ceo,command))
        self.assertEqual(self.state(),('ready',2))

    def test_replay_rejects_ended_appointment(self):
        command=self.cmd(key='replay-appointment')
        self.gateway.execute(self.ceo,command)
        self.store.db.execute("UPDATE appointments SET status='ended' WHERE id='APT-CEO'")
        self.assert_denied('AUTH_INVALID_APPOINTMENT',
            lambda:self.gateway.execute(self.ceo,command))

    def test_replay_rejects_policy_version_rotation(self):
        command=self.cmd(key='replay-version')
        self.gateway.execute(self.ceo,command)
        self.store.db.execute("UPDATE company_policy_versions SET active=0 WHERE company_id='C-A'")
        self.store.db.execute("INSERT INTO company_policy_versions VALUES (?,?,?)",
                              ('C-A','test-policy@2',1))
        self.store.db.execute("INSERT INTO company_policy_rules SELECT company_id,?,action,rule_json,fixed_cost_units FROM company_policy_rules WHERE version=?",
                              ('test-policy@2','test-policy@1'))
        self.assert_denied('AUTH_REPLAY_POLICY_CHANGED',
            lambda:self.gateway.execute(self.ceo,command))

    def test_replay_rejects_in_place_policy_rule_change(self):
        command=self.cmd(key='policy-rule-replay')
        self.gateway.execute(self.ceo,command)
        # Deliberately mutate a fixture rule without version increment. A
        # historical reply must not bypass this change even if still eligible.
        self.set_rule('task.create',role='core.ceo@1',
                      task_required=False,states=[],capabilities=[])
        self.assert_denied('AUTH_REPLAY_POLICY_CHANGED',
            lambda:self.gateway.execute(self.ceo,command))

    def test_replay_rejects_task_accountability_change(self):
        self.create_root()
        self.enable_ready()
        command=self.cmd('task.transition',
            {'task_id':'ROOT','target':'ready','expected_revision':1},key='handoff')
        self.gateway.execute(self.ceo,command)
        self.store.db.execute("UPDATE tasks SET accountable_appointment_id='APT-CTO' WHERE id='ROOT'")
        self.assert_denied('AUTH_REPLAY_SCOPE_CHANGED',
            lambda:self.gateway.execute(self.ceo,command))

    def test_replay_rejects_paused_company(self):
        command=self.cmd(key='pause-replay')
        self.gateway.execute(self.ceo,command)
        self.store.db.execute("UPDATE companies SET state='paused' WHERE id='C-A'")
        self.assert_denied('AUTH_COMPANY_NOT_ACTIVE',
            lambda:self.gateway.execute(self.ceo,command))

    def test_replay_checks_current_grant_expiry(self):
        command=self.cmd(key='expired-replay')
        self.gateway.execute(self.ceo,command)
        self.store.db.execute("UPDATE policy_grants SET expires_at=? WHERE id='G-ROOT'",
            ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
        self.assert_denied('AUTH_GRANT_EXPIRED',
            lambda:self.gateway.execute(self.ceo,command))

    def test_gateway_receipt_survives_database_restart(self):
        command=self.cmd(key='reopen-replay')
        first=self.gateway.execute(self.ceo,command)
        self.store.close()
        self.store=Store(Path(self.tmp.name)/'policy-gateway.db')
        self.host=FixtureSessionHost()
        self.ceo=self.host.issue('C-A','AGT-CEO')
        self.cto=self.host.issue('C-A','AGT-CTO')
        self.resolver=SQLitePolicyResolver(self.store)
        self.gateway=PolicyCommandGateway(self.store,self.host,self.resolver)
        self.assertEqual(self.gateway.execute(self.ceo,command),first)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM tasks WHERE id='ROOT'").fetchone()[0],1)

    def test_start_task_replay_is_not_redispatched(self):
        self.create_root()
        self.enable_ready()
        self.gateway.execute(self.ceo,self.cmd('task.transition',
            {'task_id':'ROOT','target':'ready','expected_revision':1},key='start-ready'))
        self.gateway.execute(self.ceo,self.cmd('task.transition',
            {'task_id':'ROOT','target':'assigned','expected_revision':2},key='start-assign'))
        self.set_rule('task.start',states=['assigned'])
        self.grant('G-START','APT-CEO','ROOT','task.start','task:ROOT')
        command=self.cmd('task.start',{'task_id':'ROOT','expected_revision':3},key='start-once')
        first=self.gateway.execute(self.ceo,command)
        self.assertEqual(first['state'],'in_progress')
        self.assertEqual(self.gateway.execute(self.ceo,command),first)
        self.assertEqual(self.state(),('in_progress',4))

    def test_delegation_accept_replay_preserves_one_child(self):
        self.create_root()
        self.enable_ready()
        self.gateway.execute(self.ceo,self.cmd('task.transition',
            {'task_id':'ROOT','target':'ready','expected_revision':1},key='delegate-ready'))
        self.gateway.execute(self.ceo,self.cmd('task.transition',
            {'task_id':'ROOT','target':'assigned','expected_revision':2},key='delegate-assigned'))
        self.set_rule('task.start',states=['assigned'])
        self.grant('G-START','APT-CEO','ROOT','task.start','task:ROOT')
        self.gateway.execute(self.ceo,self.cmd('task.start',
            {'task_id':'ROOT','expected_revision':3},key='delegate-start'))
        self.set_rule('delegation.offer',states=['in_progress'])
        self.grant('G-OFFER','APT-CEO','ROOT','delegation.offer','task:ROOT')
        self.gateway.execute(self.ceo,self.cmd('delegation.offer',
            {'parent_task_id':'ROOT','offer_id':'O-1','child_task_id':'CHILD',
             'target_appointment_id':'APT-CTO','objective':'Analyze fixtures',
             'expected_revision':4},key='offer-once'))
        self.set_rule('delegation.accept',role='software.cto@1',states=['in_progress'])
        self.grant('G-ACCEPT','APT-CTO','ROOT','delegation.accept','task:ROOT')
        command=self.cmd('delegation.accept',{'offer_id':'O-1','expected_revision':1},
                         appointment='APT-CTO',key='accept-once')
        first=self.gateway.execute(self.cto,command)
        self.assertEqual(first['state'],'accepted')
        self.assertEqual(self.gateway.execute(self.cto,command),first)
        self.assertEqual(self.store.db.execute(
            "SELECT count(*) FROM tasks WHERE id='CHILD'").fetchone()[0],1)

    def test_missing_active_policy_denies_root(self):
        self.store.db.execute('UPDATE company_policy_versions SET active=0')
        self.assert_denied('POLICY_NO_ACTIVE_VERSION',self.create_root)
        self.assertIsNone(self.state())

    def test_grant_revocation_rejects_without_receipt(self):
        self.store.db.execute('UPDATE policy_grants SET active=0')
        self.assert_denied('AUTH_GRANT_SCOPE',self.create_root)
        self.assertIsNone(self.state())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM command_receipts').fetchone()[0],0)

    def test_wrong_role_rejected(self):
        self.set_rule('task.create',role='software.cto@1',task_required=False,states=[])
        self.assert_denied('AUTH_ROLE_NOT_ALLOWED',self.create_root)
        self.assertIsNone(self.state())

    def test_capability_revocation_rejected(self):
        self.store.db.execute('DELETE FROM appointment_capabilities')
        self.assert_denied('AUTH_CAPABILITY_MISSING',self.create_root)

    def test_task_transition_permitted_only_after_explicit_grant(self):
        self.create_root()
        self.enable_ready()
        result=self.gateway.execute(self.ceo,self.cmd('task.transition',
              {'task_id':'ROOT','target':'ready','expected_revision':1},key='ready'))
        self.assertEqual(result['state'],'ready')
        self.assertEqual(self.state(),('ready',2))

    def test_task_transition_denied_without_grant(self):
        self.create_root()
        self.set_rule('task.transition')
        self.assert_denied('AUTH_GRANT_SCOPE',lambda:self.gateway.execute(self.ceo,
            self.cmd('task.transition',{'task_id':'ROOT','target':'ready','expected_revision':1},key='ready')))
        self.assertEqual(self.state(),('draft',1))

    def test_task_transition_stale_revision_denied(self):
        self.create_root()
        self.enable_ready()
        self.assert_denied('STALE_REVISION',lambda:self.gateway.execute(self.ceo,
            self.cmd('task.transition',{'task_id':'ROOT','target':'ready','expected_revision':9},key='stale')))
        self.assertEqual(self.state(),('draft',1))

    def test_task_state_policy_rejects_even_when_domain_transition_allows(self):
        self.create_root()
        self.set_rule('task.transition',states=['assigned'])
        self.grant('G-READY','APT-CEO','ROOT','task.transition','task:ROOT')
        self.assert_denied('AUTH_TASK_STATE',lambda:self.gateway.execute(self.ceo,
            self.cmd('task.transition',{'task_id':'ROOT','target':'ready','expected_revision':1},key='state')))
        self.assertEqual(self.state(),('draft',1))

    def test_combined_appointments_do_not_union_own_privileges(self):
        self.create_root()
        self.enable_ready()
        self.store.db.execute('INSERT INTO positions VALUES (?,?,?,?)',
                              ('C-A','POS-SHARED','software.engineer@1','POS-CEO'))
        self.store.db.execute('INSERT INTO appointments VALUES (?,?,?,?,?)',
                              ('C-A','APT-SHARED','AGT-CEO','POS-SHARED','active'))
        self.set_rule('task.transition',role='software.engineer@1',states=['draft'])
        self.assert_denied('AUTH_GRANT_SCOPE',lambda:self.gateway.execute(self.ceo,
            self.cmd('task.transition',{'task_id':'ROOT','target':'ready','expected_revision':1},
                     appointment='APT-SHARED',key='second-role')))
        self.assertEqual(self.state(),('draft',1))

    def test_policy_denial_audit_contains_no_command_content(self):
        self.store.db.execute('UPDATE policy_grants SET active=0')
        secret='PRIVATE_NOT_FOR_AUDIT'
        self.assert_denied('AUTH_GRANT_SCOPE',lambda:self.gateway.execute(self.ceo,
            self.cmd(payload={'task_id':'ROOT','objective':secret})))
        rows=self.store.db.execute('SELECT payload FROM audit_events WHERE event_type=?',
                                   ('command.denied',)).fetchall()
        self.assertEqual(len(rows),1)
        self.assertNotIn(secret,rows[0][0])
        self.assertIsNone(self.state())

    def test_pending_approval_blocks_mutation(self):
        self.create_root()
        self.set_rule('task.transition',states=['draft'],approval=True,risk='L3')
        self.grant('G-READY','APT-CEO','ROOT','task.transition','task:ROOT')
        self.assert_denied('AUTH_EXACT_APPROVAL_REQUIRED',lambda:self.gateway.execute(self.ceo,
            self.cmd('task.transition',{'task_id':'ROOT','target':'ready','expected_revision':1},key='req-app')))
        self.assertEqual(self.state(),('draft',1))

    def test_exact_approval_still_fails_closed_without_consumption(self):
        self.create_root()
        self.set_rule('task.transition',states=['draft'],approval=True,risk='L3')
        self.grant('G-READY','APT-CEO','ROOT','task.transition','task:ROOT')
        payload={'task_id':'ROOT','target':'ready','expected_revision':1}
        digest='sha256:'+hashlib.sha256(canonical({
            'company_id':'C-A','action':'task.transition','task_id':'ROOT',
            'resource':'task:ROOT','payload':payload}).encode()).hexdigest()
        self.store.db.execute('INSERT INTO policy_approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            ('C-A','APP','ROOT','task.transition','task:ROOT',digest,1,self.version,
             (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
             'approved','human','OWNER'))
        self.assert_denied('AUTH_APPROVAL_CONSUMPTION_NOT_IMPLEMENTED',lambda:
            self.gateway.execute(self.ceo,self.cmd('task.transition',payload,key='exact-app')))
        self.assertEqual(self.state(),('draft',1))

    def test_modified_approval_fingerprint_not_valid(self):
        self.create_root()
        self.set_rule('task.transition',states=['draft'],approval=True,risk='L3')
        self.grant('G-READY','APT-CEO','ROOT','task.transition','task:ROOT')
        self.store.db.execute('INSERT INTO policy_approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            ('C-A','APP','ROOT','task.transition','task:ROOT','sha256:'+'0'*64,1,self.version,
             (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
             'approved','human','OWNER'))
        self.assert_denied('AUTH_EXACT_APPROVAL_REQUIRED',lambda:self.gateway.execute(self.ceo,
            self.cmd('task.transition',{'task_id':'ROOT','target':'ready','expected_revision':1},key='bad-app')))
        self.assertEqual(self.state(),('draft',1))

    def test_revoked_appointment_rejected_on_each_command(self):
        self.store.db.execute('UPDATE appointments SET status=? WHERE company_id=? AND id=?',
                              ('ended','C-A','APT-CEO'))
        self.assert_denied('AUTH_INVALID_APPOINTMENT',self.create_root)

    def test_foreign_tenant_denied_with_no_mutation(self):
        self.assert_denied('AUTH_TENANT_MISMATCH',lambda:self.gateway.execute(self.ceo,
            self.cmd(company='C-B')))
        self.assertIsNone(self.state())

    def test_untrusted_session_denied_before_policy(self):
        self.assert_denied('AUTH_UNTRUSTED_SESSION',lambda:self.gateway.execute(
            {'agent_id':'AGT-CEO'},self.cmd()))
        self.assertIsNone(self.state())

    def test_policy_denial_does_not_change_domain_audit(self):
        self.store.db.execute('UPDATE policy_grants SET active=0')
        before=self.store.db.execute('SELECT count(*) FROM audit_events').fetchone()[0]
        self.assert_denied('AUTH_GRANT_SCOPE',self.create_root)
        after=self.store.db.execute('SELECT count(*) FROM audit_events').fetchone()[0]
        self.assertEqual(after,before+1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM tasks').fetchone()[0],0)

if __name__=='__main__': unittest.main()
