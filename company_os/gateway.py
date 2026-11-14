"""M5.3-A: local, default-deny command gateway, NOT production authentication.

Untrusted model text is an unprivileged command proposal. The trusted HOST,
not the proposal, supplies an opaque fixture session. A hostile Python caller
with direct access to Store, Workflows or FixtureSessionHost.issue() bypasses
this API; never expose those objects or this fixture issuer as a public API.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, TYPE_CHECKING
from weakref import WeakKeyDictionary

from .store import Actor, DomainError, Store
from .workflow import Workflows

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
            result = self._sessions.get(session)  # identity, never a caller-provided string
        except TypeError:
            result = None
        if result is None:
            raise DomainError('AUTH_UNTRUSTED_SESSION')
        return result

    def revoke(self, session: object) -> None:
        try:
            self._sessions.pop(session, None)
        except TypeError:
            pass


# A finite, reviewed allowlist; everything else is denied.
# str/int enforce exact primitive types, so bool does not masquerade as int.
# Optional collections are converted only after full schema validation.
ROUTES = MappingProxyType({
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
})


class CommandGateway:
    """Trusted in-process ingress; never publish Store/Workflows beside it.

    The host is responsible for authenticating a principal before issuing a
    session. This code checks the selected appointment against current SQLite
    state and enforces the existing command guards in the same transaction.
    """

    def __init__(self, store: Store, host: FixtureSessionHost, *,
                 policy_resolver: SQLitePolicyResolver | None = None):
        self._store = store
        self._host = host
        self._workflow = Workflows(store)
        # Without a resolver this remains the legacy, local-only M5.3-A fixture.
        self._policy_resolver = policy_resolver

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
        spec = ROUTES.get(action)
        if spec is None:
            raise DomainError('AUTH_UNKNOWN_ACTION')
        payload = command['payload']
        if not isinstance(payload, dict):
            raise DomainError('SCHEMA_INVALID_COMMAND')
        optional = {'evidence_ids', 'source_reports'} if action == 'report.submit' else set()
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
                if action != 'report.sources' and state['state'] != 'active':
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
                    result = self._dispatch(action, actor, p, command['key'])
                    if action != 'report.sources':
                        self._policy_resolver.save_gateway_receipt(
                            actor, action, p, command['key'], policy_info, result)
                    return result
                return self._dispatch(action, actor, p, command['key'])
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
                 policy_resolver: SQLitePolicyResolver):
        if policy_resolver is None or policy_resolver.store is not store:
            raise ValueError('A policy resolver bound to the same Store is required')
        super().__init__(store, host, policy_resolver=policy_resolver)
