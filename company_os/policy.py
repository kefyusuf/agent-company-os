"""M5.3-B1: deterministic policy decision prototype, NOT an auth or tool boundary.

All PolicyContext / ScopeGrant / ApprovalSnapshot values must be assembled by a
TRUSTED host from canonical records. A caller can forge these Python objects;
therefore this evaluator MUST NOT be exposed directly to model/user input.

A PERMIT only describes a policy check at one instant. It is not an execution
lease, credential, immutable attestation, or permission to perform an external
side effect. An execution boundary must re-evaluate state and atomically reserve
budget and consume applicable approvals before a side effect can be dispatched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
import re
from typing import Mapping


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


class Decision(str, Enum):
    PERMIT = 'PERMIT'
    DENY = 'DENY'
    APPROVAL_REQUIRED = 'APPROVAL_REQUIRED'


@dataclass(frozen=True)
class PolicyRule:
    action: str
    eligible_roles: frozenset[str]
    required_capabilities: frozenset[str] = frozenset()
    allowed_task_states: frozenset[str] = frozenset()
    allowed_company_states: frozenset[str] = frozenset({'active'})
    task_required: bool = True
    approval_required: bool = False
    independent_reviewer_required: bool = False
    risk_level: str = 'L1'

    def __post_init__(self) -> None:
        if not self.action or not self.eligible_roles:
            raise ValueError('PolicyRule requires explicit action and roles')
        for name in ('eligible_roles', 'required_capabilities', 'allowed_task_states',
                     'allowed_company_states'):
            value = getattr(self, name)
            if type(value) is not frozenset or any(type(item) is not str or not item for item in value):
                raise ValueError('Rule collections must be immutable nonempty strings')
        if self.risk_level not in {'L0', 'L1', 'L2', 'L3'}:
            raise ValueError('Unsupported risk level')
        if self.risk_level == 'L3' and not self.approval_required:
            raise ValueError('High-risk L3 operations require approval')
        if not self.allowed_company_states or not self.allowed_company_states.issubset(
            {'active', 'paused', 'inactive', 'suspended', 'archived'}
        ):
            raise ValueError('Invalid company states')
        if not self.task_required and self.allowed_task_states:
            raise ValueError('Task states on task-less rule')
        # This prototype does not allow security-incident or archival operations.
        if {'suspended', 'archived'} & self.allowed_company_states:
            raise ValueError('Suspended/archived administrative actions not modeled here')


@dataclass(frozen=True)
class PolicyBook:
    version: str
    rules: Mapping[str, PolicyRule]

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError('Policy version required')
        checked = dict(self.rules)
        for action, rule in checked.items():
            if not action or action != rule.action:
                raise ValueError('Action/rule mismatch')
        object.__setattr__(self, 'rules', MappingProxyType(checked))


@dataclass(frozen=True)
class ScopeGrant:
    """A single active appointment's bounded grant; no unions across positions."""
    company_id: str
    appointment_id: str
    task_id: str | None
    actions: frozenset[str]
    resources: frozenset[str]
    expires_at: datetime | None = None
    max_cost_units: int | None = None


@dataclass(frozen=True)
class PolicyContext:
    """Host-supplied snapshot; never deserialize this from a model's command."""
    company_id: str
    principal_company_id: str
    appointment_company_id: str
    appointment_id: str
    appointment_active: bool
    role_ref: str
    capabilities: frozenset[str]
    company_state: str
    task_id: str | None
    task_company_id: str | None
    task_state: str | None
    resource_id: str
    resource_company_id: str
    action: str
    payload_sha256: str
    target_revision: int
    requested_cost_units: int = 0
    remaining_budget_units: int = 0
    actor_agent_id: str = ''
    reviewed_author_agent_id: str | None = None
    delegated_reviewer_agent_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ApprovalSnapshot:
    company_id: str
    task_id: str | None
    action: str
    resource_id: str
    payload_sha256: str
    target_revision: int
    policy_version: str
    expires_at: datetime
    status: str
    approver_kind: str
    approver_id: str


@dataclass(frozen=True)
class PolicyOutcome:
    decision: Decision
    reason: str
    policy_version: str
    risk_level: str | None = None


class PolicyEvaluator:
    """Pure function over trusted snapshots: DENY before APPROVAL_REQUIRED."""

    def __init__(self, book: PolicyBook):
        self.book = book

    def evaluate(
        self,
        context: PolicyContext,
        grant: ScopeGrant | None,
        approval: ApprovalSnapshot | None = None,
        *,
        at: datetime | None = None,
    ) -> PolicyOutcome:
        rule = self.book.rules.get(context.action)
        risk = rule.risk_level if rule else None

        def deny(reason: str) -> PolicyOutcome:
            return PolicyOutcome(Decision.DENY, reason, self.book.version, risk)

        if rule is None:
            return deny('POLICY_UNKNOWN_ACTION')
        if at is None:
            at = datetime.now(timezone.utc)
        if not _aware(at):
            return deny('POLICY_INVALID_TIME')
        # A server-supplied snapshot must identify the same tenant at every layer.
        if (not context.company_id or context.principal_company_id != context.company_id or
            context.appointment_company_id != context.company_id or
            context.resource_company_id != context.company_id):
            return deny('AUTH_TENANT_MISMATCH')
        if not context.appointment_active or not context.appointment_id or not context.actor_agent_id:
            return deny('AUTH_INVALID_APPOINTMENT')
        if context.company_state not in rule.allowed_company_states:
            return deny('AUTH_COMPANY_STATE')
        if context.role_ref not in rule.eligible_roles:
            return deny('AUTH_ROLE_NOT_ALLOWED')
        if not rule.required_capabilities.issubset(context.capabilities):
            return deny('AUTH_CAPABILITY_MISSING')
        if rule.task_required:
            if not context.task_id or context.task_company_id != context.company_id:
                return deny('AUTH_TASK_SCOPE')
            if rule.allowed_task_states and context.task_state not in rule.allowed_task_states:
                return deny('AUTH_TASK_STATE')
        elif context.task_id is not None:
            # Strict: do not accidentally authorize unrelated task context.
            return deny('AUTH_TASK_UNEXPECTED')
        if (grant is None or grant.company_id != context.company_id or
            grant.appointment_id != context.appointment_id or
            grant.task_id != context.task_id or context.action not in grant.actions or
            not context.resource_id or context.resource_id not in grant.resources):
            return deny('AUTH_GRANT_SCOPE')
        if rule.risk_level == 'L3' and grant.expires_at is None:
            return deny('AUTH_GRANT_EXPIRY_REQUIRED')
        if grant.expires_at is not None:
            if not _aware(grant.expires_at):
                return deny('POLICY_INVALID_TIME')
            if at >= grant.expires_at:
                return deny('AUTH_GRANT_EXPIRED')
        if (type(context.requested_cost_units) is not int or
            type(context.remaining_budget_units) is not int or
            context.requested_cost_units < 0 or context.remaining_budget_units < 0):
            return deny('BUDGET_INVALID')
        if grant.max_cost_units is not None and (
            type(grant.max_cost_units) is not int or grant.max_cost_units < 0
        ):
            return deny('BUDGET_INVALID')
        if (context.requested_cost_units > context.remaining_budget_units or
            (grant.max_cost_units is not None and
             context.requested_cost_units > grant.max_cost_units)):
            return deny('BUDGET_EXCEEDED')
        if rule.independent_reviewer_required and (
            not context.reviewed_author_agent_id or
            context.actor_agent_id == context.reviewed_author_agent_id or
            context.actor_agent_id in context.delegated_reviewer_agent_ids
        ):
            return deny('CONFLICT_INDEPENDENT_REVIEW')
        if rule.approval_required:
            if (type(context.target_revision) is not int or context.target_revision < 1 or
                type(context.payload_sha256) is not str or
                re.fullmatch(r'sha256:[0-9a-f]{64}', context.payload_sha256) is None):
                return deny('SCHEMA_APPROVAL_INTENT_INVALID')
            # All hard denials happen before an approval can have any effect.
            if approval is not None and approval.status in ('denied', 'revoked'):
                return deny('AUTH_APPROVAL_REJECTED')
            if (approval is None or approval.status != 'approved' or
                approval.approver_kind != 'human' or not approval.approver_id or
                approval.company_id != context.company_id or
                approval.task_id != context.task_id or
                approval.action != context.action or
                approval.resource_id != context.resource_id or
                approval.payload_sha256 != context.payload_sha256 or
                approval.target_revision != context.target_revision or
                approval.policy_version != self.book.version or
                not _aware(approval.expires_at) or at >= approval.expires_at):
                return PolicyOutcome(Decision.APPROVAL_REQUIRED,
                                     'AUTH_EXACT_APPROVAL_REQUIRED', self.book.version, risk)
        return PolicyOutcome(Decision.PERMIT, 'POLICY_CONDITIONS_MET', self.book.version, risk)
