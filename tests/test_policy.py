"""Independent in-process policy tests: no real identity or approval issuer."""
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from company_os.policy import (ApprovalSnapshot, Decision, PolicyBook,
                               PolicyContext, PolicyEvaluator, PolicyRule, ScopeGrant)

NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


class PolicyContractTests(unittest.TestCase):
    def setUp(self):
        self.rules = {
            'report.sources': PolicyRule('report.sources', frozenset({'software.engineer@1'}),
                                          allowed_task_states=frozenset({'accepted','under_review'}),
                                          allowed_company_states=frozenset({'active','paused'}), risk_level='L0'),
            'task.start': PolicyRule('task.start', frozenset({'software.engineer@1'}),
                                     required_capabilities=frozenset({'cap.software-development'}),
                                     allowed_task_states=frozenset({'assigned'}), risk_level='L1'),
            'report.review': PolicyRule('report.review', frozenset({'software.engineer@1'}),
                                        allowed_task_states=frozenset({'under_review'}),
                                        independent_reviewer_required=True, risk_level='L2'),
            'production.deploy': PolicyRule('production.deploy', frozenset({'software.engineer@1'}),
                                           required_capabilities=frozenset({'cap.software-development'}),
                                           allowed_task_states=frozenset({'accepted'}),
                                           approval_required=True, risk_level='L3'),
        }
        self.book = PolicyBook('policy@1', self.rules)
        self.evaluator = PolicyEvaluator(self.book)
        self.ctx = PolicyContext(company_id='COMPANY-A', principal_company_id='COMPANY-A',
            appointment_company_id='COMPANY-A', appointment_id='APPT-ENG',
            appointment_active=True, role_ref='software.engineer@1',
            capabilities=frozenset({'cap.software-development'}), company_state='active',
            task_id='TASK-1', task_company_id='COMPANY-A', task_state='assigned',
            resource_id='repo://feature/TASK-1', resource_company_id='COMPANY-A',
            action='task.start', payload_sha256='sha256:'+'a'*64, target_revision=1,
            remaining_budget_units=10, requested_cost_units=2, actor_agent_id='AGT-ENG')
        self.grant = ScopeGrant(company_id='COMPANY-A', appointment_id='APPT-ENG',
            task_id='TASK-1', actions=frozenset(self.rules),
            resources=frozenset({'repo://feature/TASK-1'}), max_cost_units=5,
            expires_at=NOW+timedelta(minutes=20))

    def result(self, ctx=None, grant=None, approval=None):
        return self.evaluator.evaluate(ctx or self.ctx, self.grant if grant is None else grant,
                                       approval, at=NOW)

    def denied(self, expected, **changes):
        r=self.result(replace(self.ctx,**changes))
        self.assertEqual((r.decision,r.reason),(Decision.DENY,expected))

    def test_valid_bounded_task_permitted(self):
        self.assertEqual(self.result().decision,Decision.PERMIT)

    def test_unknown_action_default_deny(self):
        self.denied('POLICY_UNKNOWN_ACTION', action='secrets.export')

    def test_foreign_principal_denied(self):
        self.denied('AUTH_TENANT_MISMATCH', principal_company_id='COMPANY-B')

    def test_foreign_resource_denied(self):
        self.denied('AUTH_TENANT_MISMATCH', resource_company_id='COMPANY-B')

    def test_foreign_task_denied(self):
        self.denied('AUTH_TASK_SCOPE', task_company_id='COMPANY-B')

    def test_foreign_appointment_denied(self):
        self.denied('AUTH_TENANT_MISMATCH', appointment_company_id='COMPANY-B')

    def test_inactive_appointment_denied(self):
        self.denied('AUTH_INVALID_APPOINTMENT', appointment_active=False)

    def test_suspended_company_denied(self):
        self.denied('AUTH_COMPANY_STATE', company_state='suspended')

    def test_paused_company_blocks_operational_action(self):
        self.denied('AUTH_COMPANY_STATE', company_state='paused')

    def test_paused_company_allows_scoped_report_read(self):
        c=replace(self.ctx,company_state='paused',task_state='accepted',action='report.sources')
        self.assertEqual(self.result(c).decision,Decision.PERMIT)

    def test_wrong_role_denied(self):
        self.denied('AUTH_ROLE_NOT_ALLOWED',role_ref='core.ceo@1')

    def test_capability_missing_denied(self):
        self.denied('AUTH_CAPABILITY_MISSING',capabilities=frozenset())

    def test_stale_task_state_denied(self):
        self.denied('AUTH_TASK_STATE',task_state='cancelled')

    def test_missing_task_denied(self):
        self.denied('AUTH_TASK_SCOPE',task_id=None)

    def test_wrong_appointment_grant_denied(self):
        grant=replace(self.grant,appointment_id='APPT-CTO')
        self.assertEqual(self.result(grant=grant).reason,'AUTH_GRANT_SCOPE')

    def test_resource_must_be_exact(self):
        self.denied('AUTH_GRANT_SCOPE',resource_id='repo://feature/OTHER')

    def test_action_must_be_in_grant(self):
        grant=replace(self.grant,actions=frozenset({'report.review'}))
        self.assertEqual(self.result(grant=grant).reason,'AUTH_GRANT_SCOPE')

    def test_budget_exceed_denied(self):
        self.denied('BUDGET_EXCEEDED',requested_cost_units=11)

    def test_grant_budget_exceed_denied(self):
        self.denied('BUDGET_EXCEEDED',requested_cost_units=6)

    def test_negative_budget_denied(self):
        self.denied('BUDGET_INVALID',requested_cost_units=-1)

    def test_boolean_budget_denied(self):
        self.denied('BUDGET_INVALID',requested_cost_units=True)

    def test_negative_grant_max_budget_denied(self):
        grant=replace(self.grant,max_cost_units=-1)
        self.assertEqual(self.result(grant=grant).reason,'BUDGET_INVALID')

    def test_grant_expired_denied(self):
        grant=replace(self.grant,expires_at=NOW)
        self.assertEqual(self.result(grant=grant).reason,'AUTH_GRANT_EXPIRED')

    def test_grant_bad_time_type_denied(self):
        grant=replace(self.grant,expires_at='tomorrow')
        self.assertEqual(self.result(grant=grant).reason,'POLICY_INVALID_TIME')

    def test_grant_naive_time_denied(self):
        grant=replace(self.grant,expires_at=NOW.replace(tzinfo=None))
        self.assertEqual(self.result(grant=grant).reason,'POLICY_INVALID_TIME')

    def test_self_review_denied(self):
        self.denied('CONFLICT_INDEPENDENT_REVIEW',action='report.review',
                    task_state='under_review',reviewed_author_agent_id='AGT-ENG')

    def test_delegated_self_review_denied(self):
        self.denied('CONFLICT_INDEPENDENT_REVIEW',action='report.review',
                    task_state='under_review',reviewed_author_agent_id='AGT-OTHER',
                    delegated_reviewer_agent_ids=frozenset({'AGT-ENG'}))

    def test_independent_reviewer_passes(self):
        c=replace(self.ctx,action='report.review',task_state='under_review',
                  reviewed_author_agent_id='AGT-OTHER')
        self.assertEqual(self.result(c).decision,Decision.PERMIT)

    def approval(self, **changes):
        a=ApprovalSnapshot(company_id='COMPANY-A',task_id='TASK-1',action='production.deploy',
            resource_id='repo://feature/TASK-1',payload_sha256='sha256:'+'a'*64,
            target_revision=1,policy_version='policy@1',expires_at=NOW+timedelta(minutes=2),
            status='approved',approver_kind='human',approver_id='OWNER-1')
        return replace(a,**changes)

    def deploy(self, approval=None, **changes):
        c=replace(self.ctx,action='production.deploy',task_state='accepted',**changes)
        return self.result(c,approval=approval)

    def test_invalid_approval_payload_digest_denied(self):
        out=self.deploy(self.approval(),payload_sha256='literal-agent-claim')
        self.assertEqual((out.decision,out.reason),
                         (Decision.DENY,'SCHEMA_APPROVAL_INTENT_INVALID'))

    def test_invalid_approval_revision_denied(self):
        out=self.deploy(self.approval(),target_revision=0)
        self.assertEqual((out.decision,out.reason),
                         (Decision.DENY,'SCHEMA_APPROVAL_INTENT_INVALID'))

    def test_l3_grant_requires_expiry(self):
        grant=replace(self.grant,expires_at=None)
        ctx=replace(self.ctx,action='production.deploy',task_state='accepted')
        self.assertEqual(self.result(ctx,grant=grant).reason,'AUTH_GRANT_EXPIRY_REQUIRED')

    def test_approval_required_without_approval(self):
        self.assertEqual(self.deploy().decision,Decision.APPROVAL_REQUIRED)

    def test_exact_human_approval_permit(self):
        self.assertEqual(self.deploy(self.approval()).decision,Decision.PERMIT)

    def test_approval_different_revision_not_valid(self):
        self.assertEqual(self.deploy(self.approval(target_revision=2)).decision,Decision.APPROVAL_REQUIRED)

    def test_approval_different_payload_not_valid(self):
        self.assertEqual(self.deploy(self.approval(payload_sha256='sha256:'+'b'*64)).decision,Decision.APPROVAL_REQUIRED)

    def test_approval_different_company_not_valid(self):
        self.assertEqual(self.deploy(self.approval(company_id='COMPANY-B')).decision,Decision.APPROVAL_REQUIRED)

    def test_approval_bad_time_type_not_valid(self):
        self.assertEqual(self.deploy(self.approval(expires_at='tomorrow')).decision,Decision.APPROVAL_REQUIRED)

    def test_approval_expired_not_valid(self):
        self.assertEqual(self.deploy(self.approval(expires_at=NOW)).decision,Decision.APPROVAL_REQUIRED)

    def test_human_denial_is_hard_denial(self):
        out=self.deploy(self.approval(status='denied'))
        self.assertEqual((out.decision,out.reason),
                         (Decision.DENY,'AUTH_APPROVAL_REJECTED'))

    def test_revoked_human_approval_is_hard_denial(self):
        out=self.deploy(self.approval(status='revoked'))
        self.assertEqual((out.decision,out.reason),
                         (Decision.DENY,'AUTH_APPROVAL_REJECTED'))

    def test_approval_consumed_not_valid(self):
        self.assertEqual(self.deploy(self.approval(status='consumed')).decision,Decision.APPROVAL_REQUIRED)

    def test_agent_cannot_fake_human_approver(self):
        self.assertEqual(self.deploy(self.approval(approver_kind='agent')).decision,Decision.APPROVAL_REQUIRED)

    def test_policy_version_mismatch_requires_new_approval(self):
        self.assertEqual(self.deploy(self.approval(policy_version='policy@0')).decision,Decision.APPROVAL_REQUIRED)

    def test_explicit_deny_beats_valid_human_approval(self):
        r=self.deploy(self.approval(),resource_company_id='COMPANY-B')
        self.assertEqual((r.decision,r.reason),(Decision.DENY,'AUTH_TENANT_MISMATCH'))

    def test_book_rules_immutable_snapshot(self):
        self.rules['secrets.export']=PolicyRule('secrets.export',frozenset({'software.engineer@1'}))
        self.denied('POLICY_UNKNOWN_ACTION',action='secrets.export')
        with self.assertRaises(TypeError):
            self.book.rules['secrets.export']=self.rules['secrets.export']

    def test_mutable_rule_collections_rejected(self):
        with self.assertRaises(ValueError):
            PolicyRule('task.start',{'software.engineer@1'})

    def test_unapproved_L3_rule_rejected(self):
        with self.assertRaises(ValueError):
            PolicyRule('unreviewed.deploy',frozenset({'software.engineer@1'}),risk_level='L3')

    def test_misconfigured_rule_rejected(self):
        with self.assertRaises(ValueError):
            PolicyRule('x',frozenset())
        with self.assertRaises(ValueError):
            PolicyBook('v1',{'wrong':self.rules['task.start']})

    def test_no_insecure_archived_state_rule(self):
        with self.assertRaises(ValueError):
            PolicyRule('task.start',frozenset({'software.engineer@1'}),
                       allowed_company_states=frozenset({'archived'}))

if __name__=='__main__':
    unittest.main()
