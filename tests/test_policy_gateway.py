"""M5.3-B2: integration tests with the repository's real Store/CommandGateway.

These tests require the M5.3-A Store, Workflows and FixtureSessionHost.
Run against the complete GitHub branch to verify gateway integration.
"""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from company_os import DomainError, Store
from company_os.gateway import (CommandGateway, FixtureSessionHost, PolicyCommandGateway,
                                TrustedPrincipalHost)
from company_os.operations import ExternalOperationManager, ScriptedExternalAdapter
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
                 capabilities=None,approval=False,risk='L1',cost=0,company_states=None):
        if states is None:
            states=['draft']
        value={'action':action,'eligible_roles':[role],
               'allowed_task_states':states,
               'allowed_company_states':company_states or ['active'],
               'task_required':task_required,
               'required_capabilities':capabilities or [],
               'approval_required':approval,'risk_level':risk}
        self.store.db.execute('INSERT OR REPLACE INTO company_policy_rules VALUES (?,?,?,?,?)',
                              ('C-A',self.version,action,json.dumps(value),cost))

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

    def test_successful_gateway_receipt_is_append_only(self):
        self.create_root()
        before=self.store.db.execute('''SELECT result_json FROM policy_gateway_receipts
            WHERE company_id=? AND idempotency_key=?''',('C-A','c1')).fetchone()
        self.assertIsNotNone(before)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute('''UPDATE policy_gateway_receipts
                SET result_json=? WHERE company_id=? AND idempotency_key=?''',
                ('{"state":"forged"}','C-A','c1'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute('''DELETE FROM policy_gateway_receipts
                WHERE company_id=? AND idempotency_key=?''',('C-A','c1'))
        after=self.store.db.execute('''SELECT result_json FROM policy_gateway_receipts
            WHERE company_id=? AND idempotency_key=?''',('C-A','c1')).fetchone()
        self.assertEqual(after['result_json'],before['result_json'])
        self.assertEqual(self.create_root()['state'],'draft')

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

    def test_legacy_receipt_cannot_be_adopted_by_policy_gateway(self):
        # An older trusted fixture command may have succeeded without the
        # new policy ledger. Its receipt is NOT policy authorization evidence.
        command=self.cmd(key='legacy-created')
        legacy=CommandGateway(self.store,self.host)
        first=legacy.execute(self.ceo,command)
        self.assertEqual(first['state'],'draft')
        self.assert_denied('AUTH_UNATTESTED_LEGACY_RECEIPT',
            lambda:self.gateway.execute(self.ceo,command))
        self.assertEqual(self.state(),('draft',1))
        self.assertEqual(self.store.db.execute(
            "SELECT count(*) FROM policy_gateway_receipts WHERE idempotency_key='legacy-created'"
        ).fetchone()[0],0)

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

    def test_replay_rejects_task_assignment_change(self):
        self.create_root()
        self.enable_ready()
        command=self.cmd('task.transition',
            {'task_id':'ROOT','target':'ready','expected_revision':1},key='assignment-change')
        self.gateway.execute(self.ceo,command)
        self.store.db.execute("""UPDATE tasks
            SET assigned_appointment_id='APT-CTO' WHERE id='ROOT'""")
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

    def _accepted_delegation(self):
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
        return command,first

    def test_delegation_accept_replay_preserves_one_child(self):
        command,first=self._accepted_delegation()
        self.assertEqual(first['state'],'accepted')
        self.assertEqual(self.gateway.execute(self.cto,command),first)
        self.assertEqual(self.store.db.execute(
            "SELECT count(*) FROM tasks WHERE id='CHILD'").fetchone()[0],1)

    def test_delegation_replay_denies_revoked_offer(self):
        command,_=self._accepted_delegation()
        self.store.db.execute("UPDATE delegations SET state='revoked' WHERE id='O-1'")
        self.assert_denied('AUTH_REPLAY_DELEGATION_STALE',
            lambda:self.gateway.execute(self.cto,command))

    def test_delegation_replay_denies_hierarchy_change(self):
        command,_=self._accepted_delegation()
        self.store.db.execute("UPDATE positions SET manager_id=NULL WHERE id='POS-CTO'")
        self.assert_denied('AUTH_REPLAY_HIERARCHY_CHANGED',
            lambda:self.gateway.execute(self.cto,command))

    def test_delegation_replay_denies_child_accountability_change(self):
        command,_=self._accepted_delegation()
        self.store.db.execute("""UPDATE tasks
            SET accountable_appointment_id='APT-CEO' WHERE id='CHILD'""")
        self.assert_denied('AUTH_REPLAY_DELEGATION_STALE',
            lambda:self.gateway.execute(self.cto,command))

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

    def _exact_approval(self, key='exact-app', target='ready'):
        self.create_root()
        self.set_rule('task.transition',states=['draft'],approval=True,risk='L3')
        self.grant('G-READY','APT-CEO','ROOT','task.transition','task:ROOT')
        payload={'task_id':'ROOT','target':target,'expected_revision':1}
        digest='sha256:'+hashlib.sha256(canonical({
            'company_id':'C-A','action':'task.transition','task_id':'ROOT',
            'resource':'task:ROOT','payload':payload}).encode()).hexdigest()
        self.store.db.execute('''INSERT INTO policy_approvals (company_id,id,task_id,
            action,resource_id,payload_sha256,target_revision,policy_version,
            expires_at,status,approver_kind,approver_id,consumed_by_receipt)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('C-A','APP','ROOT','task.transition','task:ROOT',digest,1,self.version,
             (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
             'approved','human','OWNER',None))
        return payload

    def test_exact_approval_executes_and_consumes(self):
        payload=self._exact_approval()
        result=self.gateway.execute(self.ceo,self.cmd('task.transition',payload,key='exact-app'))
        self.assertEqual(result['state'],'ready')
        self.assertEqual(self.state(),('ready',2))
        row=self.store.db.execute('''SELECT status,consumed_by_receipt FROM policy_approvals
            WHERE company_id=? AND id=?''',('C-A','APP')).fetchone()
        self.assertEqual((row['status'],row['consumed_by_receipt']),('consumed','exact-app'))
        consumed=self.store.db.execute('''SELECT count(*) FROM audit_events
            WHERE event_type='approval.consumed' ''').fetchone()[0]
        self.assertEqual(consumed,1)

    def test_approval_gated_command_replays_without_reconsumption(self):
        payload=self._exact_approval()
        command=self.cmd('task.transition',payload,key='exact-app')
        first=self.gateway.execute(self.ceo,command)
        self.assertEqual(first['state'],'ready')
        self.assertEqual(self.gateway.execute(self.ceo,command),first)
        receipts=self.store.db.execute('''SELECT count(*) FROM policy_gateway_receipts
            WHERE company_id=? AND idempotency_key=?''',('C-A','exact-app')).fetchone()[0]
        self.assertEqual(receipts,1)
        consumed_audit=self.store.db.execute('''SELECT count(*) FROM audit_events
            WHERE event_type='approval.consumed' ''').fetchone()[0]
        self.assertEqual(consumed_audit,1)

    def test_consumed_approval_cannot_authorize_new_key(self):
        payload=self._exact_approval()
        self.gateway.execute(self.ceo,self.cmd('task.transition',payload,key='first-use'))
        # Same payload under a new key: the revision precondition denies first.
        self.assert_denied('STALE_REVISION',lambda:self.gateway.execute(
            self.ceo,self.cmd('task.transition',payload,key='second-use')))
        # A fresh payload under a new key cannot reuse the consumed fingerprint;
        # this transition is domain-valid and is blocked by approval state only.
        self.set_rule('task.transition',states=['draft','ready'],approval=True,risk='L3')
        shifted={'task_id':'ROOT','target':'assigned','expected_revision':2}
        self.assert_denied('AUTH_EXACT_APPROVAL_REQUIRED',lambda:self.gateway.execute(
            self.ceo,self.cmd('task.transition',shifted,key='third-use')))
        self.assertEqual(self.state(),('ready',2))

    def test_approval_rolls_back_to_approved_on_domain_failure(self):
        payload=self._exact_approval(target='accepted')
        self.assert_denied('STATE_TRANSITION_NOT_IMPLEMENTED_OR_FORBIDDEN',lambda:
            self.gateway.execute(self.ceo,self.cmd('task.transition',payload,key='doomed')))
        row=self.store.db.execute('''SELECT status,consumed_by_receipt FROM policy_approvals
            WHERE company_id=? AND id=?''',('C-A','APP')).fetchone()
        self.assertEqual((row['status'],row['consumed_by_receipt']),('approved',None))
        self.assertEqual(self.state(),('draft',1))
        self.assertEqual(self.store.db.execute('''SELECT count(*) FROM policy_gateway_receipts
            WHERE company_id=? AND idempotency_key=?''',('C-A','doomed')).fetchone()[0],0)

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

    def _budget(self):
        row=self.store.db.execute(
            'SELECT remaining_units FROM policy_budgets WHERE company_id=?',
            ('C-A',)).fetchone()
        return row['remaining_units']

    def test_positive_cost_reserves_and_replays_without_recharge(self):
        self.store.db.execute('INSERT INTO policy_budgets VALUES (?,?)',('C-A',5))
        self.set_rule('task.create',role='core.ceo@1',task_required=False,
                      states=[],capabilities=['cap.planning'],cost=2)
        self.store.db.execute('UPDATE policy_grants SET max_cost_units=2 WHERE company_id=? AND id=?',
                              ('C-A','G-ROOT'))
        command=self.cmd(key='costly')
        first=self.gateway.execute(self.ceo,command)
        self.assertEqual(first['state'],'draft')
        self.assertEqual(self._budget(),3)
        self.assertEqual(self.gateway.execute(self.ceo,command),first)
        self.assertEqual(self._budget(),3)
        receipt=self.store.db.execute('''SELECT reserved_cost_units
            FROM policy_gateway_receipts WHERE company_id=? AND idempotency_key=?''',
            ('C-A','costly')).fetchone()
        self.assertEqual(receipt['reserved_cost_units'],2)

    def test_positive_cost_denied_when_pool_exhausted(self):
        self.store.db.execute('INSERT INTO policy_budgets VALUES (?,?)',('C-A',1))
        self.set_rule('task.create',role='core.ceo@1',task_required=False,
                      states=[],capabilities=['cap.planning'],cost=2)
        self.store.db.execute('UPDATE policy_grants SET max_cost_units=2 WHERE company_id=? AND id=?',
                              ('C-A','G-ROOT'))
        self.assert_denied('BUDGET_EXCEEDED',lambda:self.gateway.execute(
            self.ceo,self.cmd(key='too-costly')))
        self.assertEqual(self._budget(),1)
        self.assertIsNone(self.state())

    def _submitted_report(self):
        command,first=self._accepted_delegation()
        self.assertEqual(first['state'],'accepted')
        self.set_rule('task.start',role='software.cto@1',states=['assigned'])
        self.grant('G-CSTART','APT-CTO','CHILD','task.start','task:CHILD')
        self.gateway.execute(self.cto,self.cmd('task.start',
            {'task_id':'CHILD','expected_revision':1},appointment='APT-CTO',key='child-start'))
        self.set_rule('evidence.add',role='software.cto@1',states=['in_progress'])
        self.grant('G-EVID','APT-CTO','CHILD','evidence.add','task:CHILD')
        self.gateway.execute(self.cto,self.cmd('evidence.add',
            {'task_id':'CHILD','evidence_id':'E-1','source_ref':'fixture://source',
             'content_sha256':'a'*64},appointment='APT-CTO',key='evidence-1'))
        self.set_rule('report.submit',role='software.cto@1',states=['in_progress'])
        self.grant('G-REPORT','APT-CTO','CHILD','report.submit','task:CHILD')
        report_cmd=self.cmd('report.submit',
            {'task_id':'CHILD','report_id':'R-CHILD','version':1,'content':'Child analysis',
             'expected_revision':2,'evidence_ids':['E-1']},
            appointment='APT-CTO',key='report-1')
        return report_cmd,self.gateway.execute(self.cto,report_cmd)

    def test_report_submit_records_contributor_lineage(self):
        report_cmd,first=self._submitted_report()
        self.assertEqual(first['state'],'submitted')
        rows=self.store.db.execute('''SELECT contributor_agent_id,contribution_kind
            FROM report_contributor_lineage
            WHERE company_id=? AND report_id=? AND report_version=?
            ORDER BY contributor_agent_id''',('C-A','R-CHILD',1)).fetchall()
        self.assertEqual([(r['contributor_agent_id'],r['contribution_kind']) for r in rows],
            [('AGT-CEO','delegator'),('AGT-CTO','author')])

    def test_report_lineage_is_append_only_at_gateway(self):
        self._submitted_report()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute('''UPDATE report_contributor_lineage
                SET contribution_kind='delegator'
                WHERE company_id=? AND report_id=?''',('C-A','R-CHILD'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute('''DELETE FROM report_contributor_lineage
                WHERE company_id=? AND report_id=?''',('C-A','R-CHILD'))


    def _trusted_host(self):
        host=TrustedPrincipalHost()
        host.provision('PRINCIPAL-CEO','C-A','AGT-CEO','ceo-pass-123')
        host.provision('PRINCIPAL-CTO','C-A','AGT-CTO','cto-pass-456')
        return host

    def test_trusted_host_requires_verified_credential(self):
        host=self._trusted_host()
        with self.assertRaises(DomainError) as caught:
            host.issue('PRINCIPAL-CEO','wrong-password')
        self.assertEqual(caught.exception.code,'AUTH_BAD_CREDENTIAL')
        with self.assertRaises(DomainError) as caught:
            host.issue('unprovisioned','whatever')
        self.assertEqual(caught.exception.code,'AUTH_BAD_CREDENTIAL')
        session=host.issue('PRINCIPAL-CEO','ceo-pass-123')
        gateway=PolicyCommandGateway(self.store,host,self.resolver)
        result=gateway.execute(session,self.cmd(key='trusted-ok'))
        self.assertEqual(result['state'],'draft')
        self.assert_denied('AUTH_UNTRUSTED_SESSION',lambda:gateway.execute(
            'forged-session',self.cmd(key='forged')))

    def test_credential_stored_as_salted_digest(self):
        host=TrustedPrincipalHost()
        host.provision('P','C-A','AGT-CEO','s3cret-value')
        entry=host._registry['P']
        self.assertNotEqual(entry['digest'],'s3cret-value')
        self.assertNotIn('s3cret-value',entry['digest'])
        self.assertTrue(host.issue('P','s3cret-value') is not None)

    def test_disabled_principal_invalidates_issued_sessions(self):
        host=self._trusted_host()
        session=host.issue('PRINCIPAL-CEO','ceo-pass-123')
        gateway=PolicyCommandGateway(self.store,host,self.resolver)
        gateway.execute(session,self.cmd(key='ok-before-disable'))
        host.disable('PRINCIPAL-CEO')
        self.assert_denied('AUTH_PRINCIPAL_DISABLED',lambda:gateway.execute(
            session,self.cmd(key='after-disable')))

    def test_operation_dispatch_end_to_end(self):
        adapter=ScriptedExternalAdapter()
        adapter.script('external.demo','succeeded')
        gateway=PolicyCommandGateway(self.store,self.host,self.resolver,
            operations=ExternalOperationManager(self.store,adapter))
        self.store.db.execute('INSERT INTO policy_budgets VALUES (?,?)',('C-A',10))
        self.set_rule('operation.dispatch',role='core.ceo@1',task_required=False,
                      states=[],capabilities=['cap.planning'],cost=2)
        self.grant('G-EXT','APT-CEO',None,'operation.dispatch','external:demo')
        self.store.db.execute('UPDATE policy_grants SET max_cost_units=5 WHERE company_id=? AND id=?',
                              ('C-A','G-EXT'))
        command=self.cmd('operation.dispatch',
            {'attempt_id':'ATT-G1','target_action':'external.demo',
             'target_resource':'external:demo','target_payload':{'x':1}},key='op-1')
        result=gateway.execute(self.ceo,command)
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(self.store.db.execute(
            "SELECT state FROM operation_attempts WHERE company_id='C-A' AND id='ATT-G1'").fetchone()['state'],
            'succeeded')
        self.assertEqual(self.store.db.execute(
            'SELECT remaining_units FROM policy_budgets WHERE company_id=?',
            ('C-A',)).fetchone()['remaining_units'],8)

    def test_operation_reconcile_end_to_end(self):
        adapter=ScriptedExternalAdapter()
        adapter.script('external.demo','unknown')
        gateway=PolicyCommandGateway(self.store,self.host,self.resolver,
            operations=ExternalOperationManager(self.store,adapter))
        self.store.db.execute('INSERT INTO policy_budgets VALUES (?,?)',('C-A',10))
        self.set_rule('operation.dispatch',role='core.ceo@1',task_required=False,
                      states=[],capabilities=['cap.planning'],cost=2)
        self.grant('G-EXT','APT-CEO',None,'operation.dispatch','external:demo')
        self.store.db.execute('UPDATE policy_grants SET max_cost_units=5 WHERE company_id=? AND id=?',
                              ('C-A','G-EXT'))
        dispatch_cmd=self.cmd('operation.dispatch',
            {'attempt_id':'ATT-G2','target_action':'external.demo',
             'target_resource':'external:demo','target_payload':{'x':1}},key='op-2')
        self.assertEqual(gateway.execute(self.ceo,dispatch_cmd)['state'],'unknown')
        self.set_rule('operation.reconcile',role='core.ceo@1',task_required=False,
                      states=[],capabilities=['cap.planning'],cost=0)
        self.grant('G-REC','APT-CEO',None,'operation.reconcile','company:C-A')
        rec_cmd=self.cmd('operation.reconcile',{'attempt_id':'ATT-G2'},key='rec-1')
        result=gateway.execute(self.ceo,rec_cmd)
        self.assertEqual(result['state'],'unresolved')
        self.assertEqual(self.store.db.execute(
            "SELECT state FROM operation_attempts WHERE company_id='C-A' AND id='ATT-G2'").fetchone()['state'],
            'unresolved')
        self.assertEqual(self.store.db.execute(
            'SELECT remaining_units FROM policy_budgets WHERE company_id=?',
            ('C-A',)).fetchone()['remaining_units'],8)
        self.assertEqual(gateway.execute(self.ceo,rec_cmd),result)

    def _decision_gateway(self,adapter):
        gateway=PolicyCommandGateway(self.store,self.host,self.resolver,
            operations=ExternalOperationManager(self.store,adapter))
        self.store.db.execute('INSERT INTO policy_budgets VALUES (?,?)',('C-A',10))
        self.set_rule('operation.dispatch',role='core.ceo@1',task_required=False,
                      states=[],capabilities=['cap.planning'],cost=2)
        self.set_rule('operation.reconcile',role='core.ceo@1',task_required=False,
                      states=[],capabilities=['cap.planning'],cost=0,
                      company_states=['active','paused'])
        self.set_rule('operation.decide',role='core.ceo@1',task_required=False,
                      states=[],capabilities=['cap.planning'],cost=0)
        self.set_rule('operation.reissue',role='core.ceo@1',task_required=False,
                      states=[],capabilities=['cap.planning'],cost=2)
        self.grant('G-EXT2','APT-CEO',None,'operation.dispatch','external:demo')
        self.grant('G-REC2','APT-CEO',None,'operation.reconcile','company:C-A')
        self.grant('G-DEC2','APT-CEO',None,'operation.decide','company:C-A')
        self.grant('G-REI2','APT-CEO',None,'operation.reissue','external:demo')
        self.store.db.execute('UPDATE policy_grants SET max_cost_units=5 WHERE company_id=?',
                              ('C-A',))
        return gateway

    def _dispatch_cmd(self,attempt_id,key):
        return self.cmd('operation.dispatch',
            {'attempt_id':attempt_id,'target_action':'external.demo',
             'target_resource':'external:demo','target_payload':{'x':1}},key=key)

    def _unresolved_via_gateway(self,gateway,adapter,attempt_id,dispatch_key,
                                reconcile_key):
        self.assertEqual(gateway.execute(self.ceo,
            self._dispatch_cmd(attempt_id,dispatch_key))['state'],'unknown')
        rec_cmd=self.cmd('operation.reconcile',{'attempt_id':attempt_id},
            key=reconcile_key)
        self.assertEqual(gateway.execute(self.ceo,rec_cmd)['state'],'unresolved')
        return rec_cmd

    def test_operation_decide_and_reissue_end_to_end(self):
        adapter=ScriptedExternalAdapter()
        adapter.script('external.demo','unknown')
        gateway=self._decision_gateway(adapter)
        self._unresolved_via_gateway(gateway,adapter,'ATT-G3','op-3','rec-3')
        decide_cmd=self.cmd('operation.decide',
            {'attempt_id':'ATT-G3','decision_id':'DEC-G1','decision':'reissue',
             'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()},
            key='dec-1')
        self.assertEqual(gateway.execute(self.ceo,decide_cmd)['decision'],'reissue')
        adapter.script('external.demo','succeeded')
        reissue_cmd=self.cmd('operation.reissue',
            {'attempt_id':'ATT-G3','decision_id':'DEC-G1','new_attempt_id':'ATT-G4',
             'target_action':'external.demo','target_resource':'external:demo',
             'target_payload':{'x':1}},key='rei-1')
        result=gateway.execute(self.ceo,reissue_cmd)
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(result['attempt_id'],'ATT-G4')
        rows=self.store.db.execute('''SELECT id,state FROM operation_attempts
            WHERE company_id='C-A' ORDER BY id''').fetchall()
        self.assertEqual([(r['id'],r['state']) for r in rows],
            [('ATT-G3','unresolved'),('ATT-G4','succeeded')])
        # Original hold (2) stays; the reissue reservation (2) is consumed.
        self.assertEqual(self.store.db.execute(
            'SELECT remaining_units FROM policy_budgets WHERE company_id=?',
            ('C-A',)).fetchone()['remaining_units'],6)
        # Both new commands receipt; replays return the committed results
        # without recording decisions or dispatching again.
        self.assertEqual(gateway.execute(self.ceo,decide_cmd)['decision'],'reissue')
        self.assertEqual(gateway.execute(self.ceo,reissue_cmd),result)
        self.assertEqual(self.store.db.execute(
            'SELECT count(*) FROM operation_attempts').fetchone()[0],2)
        self.assertEqual(self.store.db.execute(
            'SELECT count(*) FROM operation_reconciliations').fetchone()[0],1)

    def test_paused_company_denies_dispatch_and_reissue_allows_reconcile(self):
        adapter=ScriptedExternalAdapter()
        adapter.script('external.demo','unknown')
        gateway=self._decision_gateway(adapter)
        rec_cmd=self._unresolved_via_gateway(gateway,adapter,'ATT-P1','op-p','rec-p')
        self.store.db.execute("UPDATE companies SET state='paused' WHERE id='C-A'")
        # No new dispatches, no redispatches, no new decisions while paused.
        self.assert_denied('AUTH_COMPANY_NOT_ACTIVE',lambda:gateway.execute(
            self.ceo,self._dispatch_cmd('ATT-P2','op-p2')))
        reissue_cmd=self.cmd('operation.reissue',
            {'attempt_id':'ATT-P1','decision_id':'DEC-P1','new_attempt_id':'ATT-P3',
             'target_action':'external.demo','target_resource':'external:demo',
             'target_payload':{'x':1}},key='rei-p')
        self.assert_denied('AUTH_COMPANY_NOT_ACTIVE',lambda:gateway.execute(
            self.ceo,reissue_cmd))
        decide_cmd=self.cmd('operation.decide',
            {'attempt_id':'ATT-P1','decision_id':'DEC-P1','decision':'reissue',
             'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()},
            key='dec-p')
        self.assert_denied('AUTH_COMPANY_NOT_ACTIVE',lambda:gateway.execute(
            self.ceo,decide_cmd))
        # Reconciliation resolves committed reality and stays reachable:
        # an exact replay of the committed reconcile re-authorizes under the
        # paused-company rule and returns the committed result.
        self.assertEqual(gateway.execute(self.ceo,rec_cmd)['state'],'unresolved')
        self.assertEqual(self.store.db.execute(
            "SELECT state FROM operation_attempts WHERE company_id='C-A' AND id='ATT-P1'"
        ).fetchone()['state'],'unresolved')
        self.assertEqual(self.store.db.execute(
            'SELECT count(*) FROM operation_attempts').fetchone()[0],1)

    def test_suspended_company_denies_reconcile(self):
        adapter=ScriptedExternalAdapter()
        adapter.script('external.demo','unknown')
        gateway=self._decision_gateway(adapter)
        self._unresolved_via_gateway(gateway,adapter,'ATT-S1','op-s','rec-s')
        self.store.db.execute("UPDATE companies SET state='suspended' WHERE id='C-A'")
        # The evaluator invariant never allows suspended/archived company
        # states, so the reconcile exemption cannot become a side door.
        self.assert_denied('AUTH_COMPANY_STATE',lambda:gateway.execute(
            self.ceo,self.cmd('operation.reconcile',{'attempt_id':'ATT-S1'},
                              key='rec-s2')))

if __name__ == '__main__':
    unittest.main()
