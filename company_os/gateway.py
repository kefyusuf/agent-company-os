"""M5.3-A: local, default-deny command gateway, NOT production authentication.

Untrusted model text is an unprivileged command proposal. The trusted HOST,
not the proposal, supplies an opaque fixture session. A hostile Python caller
with direct access to Store, Workflows or FixtureSessionHost.issue() bypasses
this API; never expose those objects or this fixture issuer as a public API.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, TYPE_CHECKING
from weakref import WeakKeyDictionary

from .store import Actor, DomainError, Store
from .workflow import Workflows
from .operations import ExternalOperationManager

if TYPE_CHECKING:
    from .policy_store import SQLitePolicyResolver


@dataclass(frozen=True)
class _Principal:
    company_id: str
    agent_id: str


class _Session:
    """Opaque, identity-only local capability; not a network credential."""
    __slots__ = ('__weakref__',)


class FixtureSessionHost:
    """TRUSTED TEST HARNESS ONLY; callers with access to issue() can impersonate.

    Production must replace this issuer with a transport-authenticated host.
    Session objects must never be serialized, sent to a model or exposed to a user.
    Quarantined to legacy M5.3-A test paths; new integration tests use
    TrustedPrincipalHost.
    """

    def __init__(self):
        self._sessions: WeakKeyDictionary[_Session, _Principal] = WeakKeyDictionary()

    def issue(self, company_id: str, agent_id: str) -> object:
        if not company_id or not agent_id:
            raise DomainError('AUTH_INVALID_FIXTURE_PRINCIPAL')
        session = _Session()
        self._sessions[session] = _Principal(company_id, agent_id)
        return session

    def resolve(self, session: object) -> _Principal:
        try:
            # identity, never a caller-provided string; the session object is
            # deliberately opaque, so the weak-dict call ignores narrow typing.
            result = self._sessions.get(session)  # type: ignore[call-overload]
        except TypeError:
            result = None
        if result is None:
            raise DomainError('AUTH_UNTRUSTED_SESSION')
        return result

    def revoke(self, session: object) -> None:
        try:
            self._sessions.pop(session, None)  # type: ignore[call-overload]
        except TypeError:
            pass


class TrustedPrincipalHost:
    """M5.3-C1: fixture-grade credential-verified principal host.

    Sessions are issued ONLY after verifying a provisioned credential, so a
    process holder can no longer mint sessions for arbitrary principals.
    This is NOT production authentication: the registry lives in this process,
    credentials arrive from the trusted test transport, and no network, IAM
    or secret store exists here.
    """

    def __init__(self):
        self._sessions: WeakKeyDictionary[_Session, tuple[str, _Principal]] = \
            WeakKeyDictionary()
        self._registry: dict[str, dict] = {}

    def provision(self, principal_ref: str, company_id: str, agent_id: str,
                  credential: str) -> None:
        """Trusted out-of-band registration; never reachable from commands."""
        if not principal_ref or not company_id or not agent_id or not credential:
            raise DomainError('AUTH_INVALID_PRINCIPAL_PROVISION')
        salt = secrets.token_hex(16)
        digest = hashlib.sha256((salt + credential).encode('utf-8')).hexdigest()
        self._registry[principal_ref] = {
            'company_id': company_id, 'agent_id': agent_id,
            'salt': salt, 'digest': digest, 'enabled': True,
        }

    def disable(self, principal_ref: str) -> None:
        """Revocation propagates: issued sessions of a disabled principal die."""
        entry = self._registry.get(principal_ref)
        if entry is not None:
            entry['enabled'] = False

    def issue(self, principal_ref: str, credential: str) -> object:
        entry = self._registry.get(principal_ref)
        if entry is None or not entry['enabled']:
            raise DomainError('AUTH_BAD_CREDENTIAL')
        candidate = hashlib.sha256(
            (entry['salt'] + credential).encode('utf-8')).hexdigest()
        if not hmac.compare_digest(candidate, entry['digest']):
            raise DomainError('AUTH_BAD_CREDENTIAL')
        session = _Session()
        self._sessions[session] = (principal_ref,
                                   _Principal(entry['company_id'], entry['agent_id']))
        return session

    def resolve(self, session: object) -> _Principal:
        try:
            # identity, never a caller string; opaque session by design.
            item = self._sessions.get(session)  # type: ignore[call-overload]
        except TypeError:
            item = None
        if item is None:
            raise DomainError('AUTH_UNTRUSTED_SESSION')
        entry = self._registry.get(item[0])
        if entry is None or not entry['enabled']:
            raise DomainError('AUTH_PRINCIPAL_DISABLED')
        return item[1]

    def revoke(self, session: object) -> None:
        try:
            self._sessions.pop(session, None)  # type: ignore[call-overload]
        except TypeError:
            pass


# A finite, reviewed allowlist; everything else is denied.
# str/int enforce exact primitive types, so bool does not masquerade as int.
# Optional collections are converted only after full schema validation.
ROUTES: Mapping[str, Mapping[str, type]] = MappingProxyType({
    'task.create': {'task_id': str, 'objective': str},
    'task.transition': {'task_id': str, 'target': str, 'expected_revision': int},
    'task.start': {'task_id': str, 'expected_revision': int},
    'task.resume': {'task_id': str, 'expected_revision': int},
    'delegation.offer': {'parent_task_id': str, 'offer_id': str, 'child_task_id': str,
                         'target_appointment_id': str, 'objective': str, 'expected_revision': int},
    'delegation.accept': {'offer_id': str, 'expected_revision': int},
    'evidence.add': {'task_id': str, 'evidence_id': str, 'source_ref': str, 'content_sha256': str},
    'report.submit': {'task_id': str, 'report_id': str, 'version': int,
                      'content': str, 'expected_revision': int},
    'report.begin_review': {'report_id': str, 'version': int},
    'report.review': {'report_id': str, 'version': int, 'review_id': str,
                      'decision': str, 'reason': str},
    'task.accept': {'task_id': str, 'expected_revision': int},
    'report.sources': {'report_id': str, 'version': int},
    'operation.dispatch': {'attempt_id': str, 'target_action': str,
                           'target_resource': str, 'target_payload': dict},
    'operation.reconcile': {'attempt_id': str},
    'operation.decide': {'attempt_id': str, 'decision_id': str,
                         'decision': str, 'expires_at': str},
    'operation.reissue': {'attempt_id': str, 'decision_id': str,
                          'new_attempt_id': str, 'target_action': str,
                          'target_resource': str, 'target_payload': dict},
    'operation.recover': {'attempt_id': str},
})


class CommandGateway:
    """Trusted in-process ingress; never publish Store/Workflows beside it.

    The host is responsible for authenticating a principal before issuing a
    session. This code checks the selected appointment against current SQLite
    state and enforces the existing command guards in the same transaction.
    """

    def __init__(self, store: Store, host: FixtureSessionHost, *,
                 policy_resolver: SQLitePolicyResolver | None = None,
                 operations: ExternalOperationManager | None = None):
        self._store = store
        self._host = host
        self._workflow = Workflows(store)
        # Without a resolver this remains the legacy, local-only M5.3-A fixture.
        self._policy_resolver = policy_resolver
        self._operations = operations

    @staticmethod
    def _validate(command: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        if not isinstance(command, dict) or set(command) != {
            'type', 'company_id', 'appointment_id', 'key', 'payload'
        }:
            raise DomainError('SCHEMA_INVALID_COMMAND')
        if any(type(command[name]) is not str or not command[name]
               for name in ('type', 'company_id', 'appointment_id', 'key')):
            raise DomainError('SCHEMA_INVALID_COMMAND')
        action = command['type']
        spec: Mapping[str, type] | None = ROUTES.get(action)
        if spec is None:
            raise DomainError('AUTH_UNKNOWN_ACTION')
        payload = command['payload']
        if not isinstance(payload, dict):
            raise DomainError('SCHEMA_INVALID_COMMAND')
        optional: set[str] = (
            {'evidence_ids', 'source_reports'} if action == 'report.submit' else set())
        if not set(spec).issubset(payload) or set(payload) - set(spec) - optional:
            raise DomainError('SCHEMA_INVALID_PAYLOAD')
        if any(type(payload[k]) is not t for k, t in spec.items()):
            raise DomainError('SCHEMA_INVALID_PAYLOAD')
        for name in ('evidence_ids', 'source_reports'):
            if name not in payload:
                continue
            val = payload[name]
            if name == 'evidence_ids':
                if not isinstance(val, (tuple, list)) or any(type(s) is not str or not s for s in val):
                    raise DomainError('SCHEMA_INVALID_PAYLOAD')
            else:
                if not isinstance(val, (tuple, list)) or any(
                    not isinstance(pair, (tuple, list)) or len(pair) != 2 or
                    type(pair[0]) is not str or not pair[0] or
                    type(pair[1]) is not int or pair[1] < 1 for pair in val
                ):
                    raise DomainError('SCHEMA_INVALID_PAYLOAD')
        return action, dict(payload)

    def execute(self, session: object, command: Mapping[str, Any]) -> dict:
        # Resolve the authenticated identity OUTSIDE all client-controlled data.
        principal = self._host.resolve(session)
        action = 'invalid'
        # T-PR1: `operation.dispatch` and `operation.reissue` authorize inside
        # the command transaction (phase 1) and settle in a second transaction
        # after that commit (phase 2) — commit-then-dispatch, G-T1.
        two_phase: tuple[dict, dict, str] | None = None
        try:
            action, p = self._validate(command)
            if command['company_id'] != principal.company_id:
                raise DomainError('AUTH_TENANT_MISMATCH')

            # One BEGIN IMMEDIATE protects policy preflight + domain mutation.
            # Domain functions create nested SAVEPOINTs through Store.tx().
            with self._store.tx():
                company = principal.company_id
                actor = Actor(company, principal.agent_id, command['appointment_id'])
                self._store.require_actor(actor, company)
                state = self._store.db.execute(
                    'SELECT state FROM companies WHERE id=?', (company,)
                ).fetchone()
                if state is None:
                    raise DomainError('REFERENCE_COMPANY_NOT_FOUND')
                # FLOW-04 (D-PR3): a paused/suspended company has no new
                # dispatches and no redispatches. Reconciliation is exempted
                # from this hard gate because it resolves committed reality
                # without creating new effects; the policy layer remains the
                # authority (its rules may allow `paused`, and the evaluator
                # invariant never allows `suspended`/`archived`).
                if (action not in ('report.sources', 'operation.reconcile')
                        and state['state'] != 'active'):
                    raise DomainError('AUTH_COMPANY_NOT_ACTIVE')
                if action == 'task.create':
                    # A new root Task can only be opened from the CEO seat;
                    # any child task must pass the authorized Delegation path.
                    role = self._store.db.execute('''
                        SELECT p.role_ref FROM positions p JOIN appointments a
                        ON p.company_id=a.company_id AND p.id=a.position_id
                        WHERE a.company_id=? AND a.id=?
                    ''', (company, actor.appointment_id)).fetchone()
                    if role is None or role['role_ref'] != 'core.ceo@1':
                        raise DomainError('AUTH_ROOT_TASK_REQUIRES_CEO')
                if self._policy_resolver is not None:
                    # An exact replay is a historical result lookup, NOT a new
                    # execution. Revalidate current identity, grants, policy
                    # and resource scope before returning the committed result.
                    cached = self._policy_resolver.replay(
                        actor, action, p, command['key'])
                    if cached is not None:
                        return cached
                    # The old fixture gateway used only domain receipts, without
                    # an authoritative policy snapshot. Never retroactively
                    # adopt such a successful command as policy-authorized.
                    legacy = self._store.db.execute(
                        'SELECT 1 FROM command_receipts WHERE company_id=? AND key=?',
                        (company, command['key'])
                    ).fetchone()
                    if legacy is not None:
                        raise DomainError('AUTH_UNATTESTED_LEGACY_RECEIPT')
                    # For a first execution, check current Task/Delegation
                    # revisions and permitted states. Receipt, business writes
                    # and audit all commit in this same outer SQLite tx.
                    policy_info = self._policy_resolver.enforce(actor, action, p)
                    approval_id = policy_info.get('approval_id')
                    if approval_id is not None:
                        # One-time consumption binds this approval to this
                        # command attempt, atomically with the mutation.
                        self._policy_resolver.consume_approval(
                            actor, approval_id, command['key'], action)
                    if action == 'operation.dispatch':
                        if self._operations is None:
                            raise DomainError('OPERATIONS_NOT_CONFIGURED')
                        # T-PR1 phase 1: guards, transport identity and
                        # planned→authorized commit with THIS transaction.
                        self._operations.authorize_dispatch(
                            actor, p, policy_info, command['key'])
                        two_phase = (p, p, command['key'])
                    elif action == 'operation.reconcile':
                        if self._operations is None:
                            raise DomainError('OPERATIONS_NOT_CONFIGURED')
                        result = self._operations.reconcile(
                            actor, p, command['key'])
                    elif action == 'operation.decide':
                        if self._operations is None:
                            raise DomainError('OPERATIONS_NOT_CONFIGURED')
                        result = self._operations.decide(
                            actor, p, policy_info, command['key'])
                    elif action == 'operation.reissue':
                        if self._operations is None:
                            raise DomainError('OPERATIONS_NOT_CONFIGURED')
                        # T-PR1 phase 1: consumption + new-attempt authorization
                        # commit here; the re-dispatched effect settles below.
                        self._operations.reissue(
                            actor, p, policy_info, command['key'])
                        two_phase = (p, {'attempt_id': p['new_attempt_id'],
                                         'target_action': p['target_action'],
                                         'target_resource': p['target_resource'],
                                         'target_payload': p['target_payload']},
                                     command['key'])
                    elif action == 'operation.recover':
                        if self._operations is None:
                            raise DomainError('OPERATIONS_NOT_CONFIGURED')
                        result = self._operations.recover(
                            actor, p, command['key'])
                    else:
                        result = self._dispatch(action, actor, p, command['key'])
                        if action == 'report.submit':
                            # Host-attested contributor lineage, recorded from
                            # canonical records in the same transaction.
                            self._policy_resolver.record_report_lineage(
                                actor, p['report_id'], p['version'])
                    if action != 'report.sources' and two_phase is None:
                        self._policy_resolver.save_gateway_receipt(
                            actor, action, p, command['key'], policy_info, result)
                    if two_phase is None:
                        return result
                else:
                    return self._dispatch(action, actor, p, command['key'])
            # The phase-1 authorization commit is durable here (G-T1). Phase 2
            # runs outside it (owner decision Q2): effect first, then a short
            # settlement transaction that also writes the append-only receipt.
            if two_phase is not None:
                if self._operations is None:
                    # Unreachable: two_phase is only set under a configured
                    # manager; kept as an explicit fail-closed guard.
                    raise DomainError('OPERATIONS_NOT_CONFIGURED')
                original_p, phase2_payload, key = two_phase
                call = self._operations.execute_effect(actor, phase2_payload,
                                                       policy_info)
                with self._store.tx():
                    result = self._operations.settle_dispatch(
                        actor, phase2_payload, call, key)
                    self._policy_resolver.save_gateway_receipt(
                        actor, action, original_p, key, policy_info, result)
                return result
        except DomainError as exc:
            # A denied attempt is auditable, but never leaks raw payload or secrets.
            # Unknown sessions cannot be attributed and are not persisted here.
            with self._store.tx():
                exists = self._store.db.execute(
                    'SELECT 1 FROM companies WHERE id=?', (principal.company_id,)
                ).fetchone()
                if exists:
                    self._store.audit(principal.company_id, 'command', 'denied',
                                      'command.denied', f'agent:{principal.agent_id}',
                                      {'action': action, 'code': exc.code})
            raise

    def _dispatch(self, action: str, actor: Actor, p: dict, key: str) -> dict:
        s, w = self._store, self._workflow
        if action == 'task.create':
            return s.create_task(actor, p['task_id'], p['objective'],
                                 actor.appointment_id, actor.appointment_id, key)
        if action == 'task.transition':
            return s.transition_task(actor, p['task_id'], p['target'], p['expected_revision'], key)
        if action == 'task.start':
            return w.start_task(actor, p['task_id'], p['expected_revision'], key)
        if action == 'task.resume':
            return w.resume_task(actor, p['task_id'], p['expected_revision'], key)
        if action == 'delegation.offer':
            return w.offer_delegation(actor, p['parent_task_id'], p['offer_id'], p['child_task_id'],
                                      p['target_appointment_id'], p['objective'], p['expected_revision'], key)
        if action == 'delegation.accept':
            return w.accept_delegation(actor, p['offer_id'], p['expected_revision'], key)
        if action == 'evidence.add':
            return w.add_evidence(actor, p['task_id'], p['evidence_id'], p['source_ref'],
                                  p['content_sha256'], key)
        if action == 'report.submit':
            evidence = tuple(p.get('evidence_ids', ()))
            refs = tuple((v[0], v[1]) for v in p.get('source_reports', ()))
            return w.submit_report(actor, p['task_id'], p['report_id'], p['version'],
                                   p['content'], p['expected_revision'], key,
                                   evidence_ids=evidence, source_reports=refs)
        if action == 'report.begin_review':
            return w.begin_review(actor, p['report_id'], p['version'], key)
        if action == 'report.review':
            return w.review_report(actor, p['report_id'], p['version'], p['review_id'],
                                   p['decision'], p['reason'], key)
        if action == 'task.accept':
            return w.accept_task(actor, p['task_id'], p['expected_revision'], key)
        if action == 'report.sources':
            return w.report_sources(actor, p['report_id'], p['version'])
        raise DomainError('AUTH_UNKNOWN_ACTION')

class PolicyCommandGateway(CommandGateway):
    """M5.3-B2: fixture commands MUST pass the SQLite policy gate.

    This class still relies on the trusted TEST host; it is not a public API,
    network authenticator, or permission to perform external side effects.
    """

    def __init__(self, store: Store, host: FixtureSessionHost,
                 policy_resolver: SQLitePolicyResolver, *,
                 operations: ExternalOperationManager | None = None):
        if policy_resolver is None or policy_resolver.store is not store:
            raise ValueError('A policy resolver bound to the same Store is required')
        if operations is not None and operations.store is not store:
            raise ValueError('The operations manager must be bound to the same Store')
        super().__init__(store, host, policy_resolver=policy_resolver,
                         operations=operations)
