"""M5.2 deterministic workflow behavior; no LLM execution or production access."""
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from company_os import Actor, DomainError, Store
from company_os.workflow import Workflows


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'work.db'
        self.store=Store(self.path,fixture_bootstrap=True)
        self.ceo=self.store.bootstrap('COMPANY-DEMO-001',[
            ('POS-CEO','core.ceo@1',None,'AGT-CEO','APPOINT-CEO'),
            ('POS-CTO','software.cto@1','POS-CEO','AGT-CTO','APPOINT-CTO'),
            ('POS-MANAGER','software.manager@1','POS-CTO','AGT-MANAGER','APPOINT-MANAGER'),
            ('POS-BACKEND','software.backend@1','POS-MANAGER','AGT-BACKEND','APPOINT-BACKEND'),
            ('POS-RESEARCH','software.research@1','POS-BACKEND','AGT-RESEARCH','APPOINT-RESEARCH'),
        ])
        self.actors={name:Actor('COMPANY-DEMO-001','AGT-'+name,'APPOINT-'+name)
                     for name in ['CEO','CTO','MANAGER','BACKEND','RESEARCH']}
        # Test-only simulated activation; real readiness and finance coverage are NOT implemented.
        with self.store.tx():
            self.store.db.execute("UPDATE companies SET state='active' WHERE id='COMPANY-DEMO-001'")
            self.store.db.execute("UPDATE agents SET kind='temporary' WHERE id='AGT-RESEARCH'")
            self.store.audit('COMPANY-DEMO-001','company','COMPANY-DEMO-001',
                             'fixture.simulated_activation','system:fixture',{})
        self.w=Workflows(self.store)
        self.store.create_task(self.ceo,'ROOT','Investigate demo latency',
                               'APPOINT-CEO','APPOINT-CEO','root-create')
        self.store.transition_task(self.ceo,'ROOT','ready',1,'root-ready')
        self.store.transition_task(self.ceo,'ROOT','assigned',2,'root-assigned')
        self.w.start_task(self.ceo,'ROOT',3,'root-start')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def denies(self,code,func):
        with self.assertRaises(DomainError) as err:
            func()
        self.assertEqual(err.exception.code,code)

    def delegate(self,from_name,to_name,parent,child):
        source=self.actors[from_name]
        target=self.actors[to_name]
        revision=self.store.task(source,parent)['revision']
        offer_id='DEL-'+child
        offered=self.w.offer_delegation(source,parent,offer_id,child,
                                        target.appointment_id,'Do '+child,
                                        revision,'offer-'+child)
        self.assertEqual(offered['state'],'offered')
        result=self.w.accept_delegation(target,offer_id,1,'accept-'+child)
        self.assertEqual(result['state'],'accepted')
        self.w.start_task(target,child,1,'start-'+child)
        return offer_id

    def chain(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        self.delegate('CTO','MANAGER','CTO-TASK','MANAGER-TASK')
        self.delegate('MANAGER','BACKEND','MANAGER-TASK','BACKEND-TASK')
        self.delegate('BACKEND','RESEARCH','BACKEND-TASK','RESEARCH-TASK')

    def evidence_report(self,owner,task,name,version=1,sources=(),evidence=True):
        actor=self.actors[owner]
        evidence_ids=()
        if evidence:
            evidence_id='E-'+name+'-'+str(version)
            content_hash=hashlib.sha256(b'deterministic evidence').hexdigest()
            self.w.add_evidence(actor,task,evidence_id,'fixture://'+name,content_hash,'evidence-'+name+str(version))
            evidence_ids=(evidence_id,)
        revision=self.store.task(actor,task)['revision']
        self.w.submit_report(actor,task,name,version,'Evidence backed: '+name,revision,
                             'report-'+name+str(version),evidence_ids=evidence_ids,
                             source_reports=sources)

    def review_accept(self,reviewer,child_task,report):
        actor=self.actors[reviewer]
        self.w.begin_review(actor,report,1,'begin-'+report)
        self.w.review_report(actor,report,1,'REVIEW-'+report,'accepted','Source references checked',
                             'review-'+report)
        child=self.store.task(actor,child_task)
        self.w.accept_task(actor,child_task,child['revision'],'task-accept-'+child_task)

    def test_full_hierarchical_delegation_and_report_rollup(self):
        self.chain()
        self.evidence_report('RESEARCH','RESEARCH-TASK','R-RESEARCH')
        self.review_accept('BACKEND','RESEARCH-TASK','R-RESEARCH')
        self.evidence_report('BACKEND','BACKEND-TASK','R-BACKEND',
                             sources=(('R-RESEARCH',1),))
        self.review_accept('MANAGER','BACKEND-TASK','R-BACKEND')
        self.evidence_report('MANAGER','MANAGER-TASK','R-MANAGER',
                             sources=(('R-BACKEND',1),),evidence=False)
        self.review_accept('CTO','MANAGER-TASK','R-MANAGER')
        self.evidence_report('CTO','CTO-TASK','R-CTO',
                             sources=(('R-MANAGER',1),),evidence=False)
        self.review_accept('CEO','CTO-TASK','R-CTO')
        self.evidence_report('CEO','ROOT','R-CEO',
                             sources=(('R-CTO',1),),evidence=False)
        self.assertEqual(self.store.task(self.ceo,'ROOT')['state'],'submitted')
        self.assertEqual(self.w.report_sources(self.ceo,'R-CEO',1)['source_reports'],[('R-CTO',1)])
        self.assertEqual(self.store.task(self.actors['RESEARCH'],'RESEARCH-TASK')['state'],'accepted')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM delegations').fetchone()[0],4)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0],4)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM reports').fetchone()[0],5)
        # Owner approval, true external verification and real model runs remain out of scope.

    def test_create_task_cannot_bypass_delegation_with_existing_parent(self):
        self.denies('AUTH_DELEGATION_REQUIRED',lambda:self.store.create_task(
            self.ceo,'UNAUTHORIZED-CHILD','Bypass','APPOINT-CEO','APPOINT-CEO',
            'bypass-parent',parent_id='ROOT'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],1)

    def test_combined_roles_may_delegate_but_cannot_self_review(self):
        # Combining two positions is valid; independent review is still identity-based.
        self.store.db.execute("UPDATE appointments SET agent_id='AGT-CEO' WHERE id='APPOINT-CTO'")
        combined=Actor('COMPANY-DEMO-001','AGT-CEO','APPOINT-CTO')
        result=self.w.offer_delegation(self.ceo,'ROOT','COMBINED','COMBINED-TASK',
                                       'APPOINT-CTO','Scoped work',4,'combined-offer')
        self.assertEqual(result['state'],'offered')
        self.w.accept_delegation(combined,'COMBINED',1,'combined-accept')
        self.w.start_task(combined,'COMBINED-TASK',1,'combined-start')
        evidence_hash=hashlib.sha256(b'test').hexdigest()
        self.w.add_evidence(combined,'COMBINED-TASK','EV-COMB','fixture://combined',evidence_hash,'ev-comb')
        self.w.submit_report(combined,'COMBINED-TASK','R-COMB',1,'Result',2,'report-comb',
                             evidence_ids=('EV-COMB',))
        self.denies('CONFLICT_SAME_AGENT_IDENTITY',lambda:self.w.begin_review(
            self.ceo,'R-COMB',1,'begin-comb'))

    def test_only_direct_report_can_receive_delegation(self):
        self.denies('AUTH_NOT_DIRECT_REPORT',lambda:self.w.offer_delegation(
            self.ceo,'ROOT','BAD','BAD-TASK','APPOINT-BACKEND','Skip hierarchy',4,'bad-offer'))

    def test_other_agent_cannot_offer_task(self):
        self.denies('AUTH_NOT_ACCOUNTABLE',lambda:self.w.offer_delegation(
            self.actors['CTO'],'ROOT','BAD','BAD-TASK','APPOINT-MANAGER','Skip CEO',4,'bad'))

    def test_only_intended_target_accepts(self):
        self.w.offer_delegation(self.ceo,'ROOT','OFFER','CHILD','APPOINT-CTO','Work',4,'offer')
        self.denies('AUTH_NOT_OFFER_TARGET',lambda:self.w.accept_delegation(
            self.actors['BACKEND'],'OFFER',1,'wrong-target'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],1)

    def test_acceptance_atomic_and_idempotent(self):
        self.w.offer_delegation(self.ceo,'ROOT','OFFER','CHILD','APPOINT-CTO','Work',4,'offer')
        target=self.actors['CTO']
        first=self.w.accept_delegation(target,'OFFER',1,'accept')
        self.assertEqual(first,self.w.accept_delegation(target,'OFFER',1,'accept'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],2)
        self.denies('IDEMPOTENCY_CONFLICT',lambda:self.w.accept_delegation(target,'OFFER',2,'accept'))

    def test_declined_or_cancelled_parent_cannot_accept(self):
        self.w.offer_delegation(self.ceo,'ROOT','OFFER','CHILD','APPOINT-CTO','Work',4,'offer')
        self.store.db.execute("UPDATE tasks SET state='cancelled' WHERE id='ROOT'")  # fixture-only cancellation
        self.denies('STATE_PARENT_NOT_ACTIVE',lambda:self.w.accept_delegation(
            self.actors['CTO'],'OFFER',1,'accept'))

    def test_revoked_delegator_cannot_be_accepted(self):
        self.w.offer_delegation(self.ceo,'ROOT','OFFER','CHILD','APPOINT-CTO','Work',4,'offer')
        self.store.db.execute("UPDATE appointments SET status='ended' WHERE id='APPOINT-CEO'")  # fixture revoke
        self.denies('AUTH_DELEGATOR_REVOKED',lambda:self.w.accept_delegation(
            self.actors['CTO'],'OFFER',1,'accept'))

    def test_stale_parent_revision_denied(self):
        self.denies('STALE_REVISION',lambda:self.w.offer_delegation(
            self.ceo,'ROOT','OFFER','CHILD','APPOINT-CTO','Work',3,'offer'))

    def test_temporary_research_cannot_subdelegate_beyond_depth(self):
        self.chain()
        # Force a direct report only for a negative fixture at depth 4.
        with self.store.tx():
            self.store.db.execute('INSERT INTO positions VALUES (?,?,?,?)',
                                  ('COMPANY-DEMO-001','POS-EXTRA','research.extra@1','POS-RESEARCH'))
            self.store.db.execute('INSERT INTO agents VALUES (?,?,?)',
                                  ('COMPANY-DEMO-001','AGT-EXTRA','temporary'))
            self.store.db.execute('INSERT INTO appointments VALUES (?,?,?,?,?)',
                                  ('COMPANY-DEMO-001','APPOINT-EXTRA','AGT-EXTRA','POS-EXTRA','active'))
        owner=self.actors['RESEARCH']
        rev=self.store.task(owner,'RESEARCH-TASK')['revision']
        self.denies('POLICY_DELEGATION_DEPTH',lambda:self.w.offer_delegation(
            owner,'RESEARCH-TASK','EXTRA','EXTRA-TASK','APPOINT-EXTRA','Too deep',rev,'extra'))

    def test_paused_company_blocks_work_operations(self):
        self.store.db.execute("UPDATE companies SET state='paused'")  # fixture pause
        self.denies('AUTH_COMPANY_NOT_ACTIVE',lambda:self.w.offer_delegation(
            self.ceo,'ROOT','OFFER','CHILD','APPOINT-CTO','Work',4,'offer'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM delegations').fetchone()[0],0)

    def test_evidence_requires_valid_sha256(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        self.denies('SCHEMA_INVALID_EVIDENCE',lambda:self.w.add_evidence(
            self.actors['CTO'],'CTO-TASK','E1','fixture://x','not-a-hash','bad-evidence'))

    def test_report_cannot_submit_without_references(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        target=self.actors['CTO']
        rev=self.store.task(target,'CTO-TASK')['revision']
        self.denies('STATE_REPORT_REQUIRES_SOURCES',lambda:self.w.submit_report(
            target,'CTO-TASK','R-CTO',1,'Unproven claim',rev,'no-source'))

    def test_report_source_must_be_accepted_child(self):
        self.chain()
        self.evidence_report('RESEARCH','RESEARCH-TASK','R-RESEARCH')
        self.denies('STATE_SOURCE_CHILD_NOT_ACCEPTED',lambda:self.evidence_report(
            'BACKEND','BACKEND-TASK','R-BACKEND',sources=(('R-RESEARCH',1),)))

    def test_review_cannot_be_performed_by_author(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        self.evidence_report('CTO','CTO-TASK','R-CTO')
        self.denies('AUTH_NOT_REPORT_REVIEWER',lambda:self.w.begin_review(
            self.actors['CTO'],'R-CTO',1,'begin-self'))

    def test_same_identity_different_appointment_not_independent(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        self.evidence_report('CTO','CTO-TASK','R-CTO')
        # Fixture: CEO and CTO now use same permanent identity, though appointments differ.
        self.store.db.execute("UPDATE appointments SET agent_id='AGT-CTO' WHERE id='APPOINT-CEO'")
        fake_independent=Actor('COMPANY-DEMO-001','AGT-CTO','APPOINT-CEO')
        self.denies('CONFLICT_SAME_AGENT_IDENTITY',lambda:self.w.begin_review(
            fake_independent,'R-CTO',1,'begin-fake'))

    def test_review_and_task_acceptance_are_distinct(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        self.evidence_report('CTO','CTO-TASK','R-CTO')
        self.w.begin_review(self.ceo,'R-CTO',1,'begin-review')
        review=self.w.review_report(self.ceo,'R-CTO',1,'REV','accepted','Checked citations','review')
        self.assertEqual(review['task_state'],'under_review')
        self.assertEqual(self.store.task(self.ceo,'CTO-TASK')['state'],'under_review')
        self.w.accept_task(self.ceo,'CTO-TASK',5,'accept')
        self.assertEqual(self.store.task(self.ceo,'CTO-TASK')['state'],'accepted')

    def test_child_incomplete_blocks_parent_acceptance(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        self.delegate('CTO','MANAGER','CTO-TASK','MANAGER-TASK')
        self.evidence_report('CTO','CTO-TASK','R-CTO')
        self.w.begin_review(self.ceo,'R-CTO',1,'begin-review')
        self.w.review_report(self.ceo,'R-CTO',1,'REV','accepted','Checked sources','review')
        self.denies('STATE_REQUIRED_CHILD_NOT_ACCEPTED',lambda:self.w.accept_task(
            self.ceo,'CTO-TASK',5,'accept'))

    def test_accepted_child_must_be_in_parent_report_sources(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        self.delegate('CTO','MANAGER','CTO-TASK','MANAGER-TASK')
        self.evidence_report('MANAGER','MANAGER-TASK','R-MANAGER')
        self.review_accept('CTO','MANAGER-TASK','R-MANAGER')
        # CTO can cite its own evidence, but cannot suppress accepted child results.
        self.evidence_report('CTO','CTO-TASK','R-CTO')
        self.w.begin_review(self.ceo,'R-CTO',1,'begin-review')
        self.w.review_report(self.ceo,'R-CTO',1,'REV','accepted','Checked','review')
        self.denies('STATE_CHILD_EVIDENCE_NOT_LINKED',lambda:self.w.accept_task(
            self.ceo,'CTO-TASK',5,'accept'))

    def test_offered_child_blocks_parent_acceptance(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        self.w.offer_delegation(self.actors['CTO'],'CTO-TASK','OFFER','PENDING',
                                'APPOINT-MANAGER','Pending work',2,'offer')
        self.evidence_report('CTO','CTO-TASK','R-CTO')
        self.w.begin_review(self.ceo,'R-CTO',1,'begin-review')
        self.w.review_report(self.ceo,'R-CTO',1,'REV','accepted','Checked','review')
        self.denies('STATE_PENDING_DELEGATION',lambda:self.w.accept_task(
            self.ceo,'CTO-TASK',5,'accept'))

    def test_immutable_submitted_report_and_review(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        self.evidence_report('CTO','CTO-TASK','R-CTO')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE reports SET content='rewritten' WHERE id='R-CTO'")
        self.w.begin_review(self.ceo,'R-CTO',1,'begin-review')
        self.w.review_report(self.ceo,'R-CTO',1,'REV','accepted','Checked','review')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("DELETE FROM reviews WHERE id='REV'")
        self.assertEqual(self.store.db.execute("SELECT content FROM reports WHERE id='R-CTO'").fetchone()[0],
                         'Evidence backed: R-CTO')

    def test_review_requires_exact_started_report_context(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        self.evidence_report('CTO','CTO-TASK','R-CTO')
        self.w.begin_review(self.ceo,'R-CTO',1,'begin-review')
        self.denies('REFERENCE_REPORT_NOT_FOUND',lambda:self.w.review_report(
            self.ceo,'R-CTO',2,'REV','accepted','wrong version','wrong-version'))
        # A matching report exists, but only the explicitly selected one may be reviewed.
        self.store.db.execute('''INSERT INTO reports VALUES (?,?,?,?,?,?,?)''',
                              ('COMPANY-DEMO-001','R-OTHER',1,'CTO-TASK','APPOINT-CTO',
                               'AGT-CTO','Other submitted report fixture'))
        self.denies('AUTH_REVIEW_CONTEXT_MISMATCH',lambda:self.w.review_report(
            self.ceo,'R-OTHER',1,'REV','accepted','wrong report','wrong-report'))
        self.w.review_report(self.ceo,'R-CTO',1,'REV','accepted','Matched','matched-review')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM review_contexts').fetchone()[0],0)

    def test_report_revision_after_changes_requested(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        self.evidence_report('CTO','CTO-TASK','R-CTO')
        self.w.begin_review(self.ceo,'R-CTO',1,'begin-review')
        self.w.review_report(self.ceo,'R-CTO',1,'REV1','changes_requested','Need more detail','rev1')
        self.w.resume_task(self.actors['CTO'],'CTO-TASK',5,'resume-after-changes')
        self.evidence_report('CTO','CTO-TASK','R-CTO',version=2)
        self.w.begin_review(self.ceo,'R-CTO',2,'begin-review-v2')
        self.w.review_report(self.ceo,'R-CTO',2,'REV2','accepted','Checked','rev2')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM reports WHERE id=?',('R-CTO',)).fetchone()[0],2)

    def test_duplicate_submit_does_not_duplicate_events(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        cto=self.actors['CTO']
        evidence_id='E-CTO'
        self.w.add_evidence(cto,'CTO-TASK',evidence_id,'fixture://x',hashlib.sha256(b'x').hexdigest(),'ev')
        first=self.w.submit_report(cto,'CTO-TASK','R-CTO',1,'One result',2,'report',evidence_ids=(evidence_id,))
        events=len(self.store.events(cto))
        second=self.w.submit_report(cto,'CTO-TASK','R-CTO',1,'One result',2,'report',evidence_ids=(evidence_id,))
        self.assertEqual(first,second)
        self.assertEqual(len(self.store.events(cto)),events)
        self.denies('IDEMPOTENCY_CONFLICT',lambda:self.w.submit_report(
            cto,'CTO-TASK','R-CTO',1,'Changed result',2,'report',evidence_ids=(evidence_id,)))

    def test_report_reference_read_access(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        self.evidence_report('CTO','CTO-TASK','R-CTO')
        self.assertEqual(self.w.report_sources(self.ceo,'R-CTO',1)['evidence_ids'],['E-R-CTO-1'])
        self.denies('AUTH_REPORT_SCOPE',lambda:self.w.report_sources(
            self.actors['BACKEND'],'R-CTO',1))

    def test_stale_task_revision_blocks_start(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        self.denies('STALE_REVISION',lambda:self.w.start_task(self.actors['CTO'],'CTO-TASK',99,'start'))

    def test_reopen_db_preserves_report_review_and_evidence(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        self.evidence_report('CTO','CTO-TASK','R-CTO')
        self.w.begin_review(self.ceo,'R-CTO',1,'begin-review')
        self.w.review_report(self.ceo,'R-CTO',1,'REV','accepted','Checked','review')
        self.store.close()
        self.store=Store(self.path)
        self.w=Workflows(self.store)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM reports').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0],1)
        self.assertEqual(self.w.report_sources(self.ceo,'R-CTO',1)['evidence_ids'],['E-R-CTO-1'])

    def test_no_cross_tenant_delegation_or_report_read(self):
        self.delegate('CEO','CTO','ROOT','CTO-TASK')
        self.evidence_report('CTO','CTO-TASK','R-CTO')
        other=self.store.bootstrap('COMPANY-OTHER',[
            ('POS-CEO','core.ceo@1',None,'AGT-OTHER','APPOINT-OTHER'),
        ])
        self.denies('REFERENCE_REPORT_NOT_FOUND',lambda:self.w.report_sources(other,'R-CTO',1))
        self.store.db.execute("UPDATE companies SET state='active' WHERE id='COMPANY-OTHER'")  # test fixture only
        self.denies('REFERENCE_TASK_NOT_FOUND',lambda:self.w.start_task(other,'CTO-TASK',1,'cross'))


if __name__=='__main__':
    unittest.main()
