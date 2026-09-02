"""
The permission matrix — the server-side authority on who may do what.

The React sidebar hides what a role cannot use. That is presentation. THIS file
is enforcement: every route declares a capability, and the dependency in
api/deps.py checks it against the caller's role before the handler runs.

Mirrors ROLE_PERMISSIONS in the frontend, with one difference that matters:
`AUDIT_WRITE` and `AUDIT_DELETE` do not exist as capabilities at all. Nobody can
have them, so no role can be misconfigured into holding them.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    DATA_PRINCIPAL = "data_principal"
    ADMIN = "admin"          # Admin / DPO
    AUDITOR = "auditor"
    GRIEVANCE_OFFICER = "grievance_officer"


class Capability(StrEnum):
    # Self-service (the Data Principal's own data only)
    SELF_READ = "self:read"
    SELF_CONSENT_WRITE = "self:consent:write"
    SELF_DSAR_WRITE = "self:dsar:write"
    SELF_GRIEVANCE_WRITE = "self:grievance:write"

    # Compliance operations
    CONSENT_READ = "consent:read"
    CONSENT_VALIDATE = "consent:validate"
    PURPOSE_MANAGE = "purpose:manage"
    DSAR_READ = "dsar:read"
    DSAR_PROCESS = "dsar:process"
    GRIEVANCE_READ = "grievance:read"
    GRIEVANCE_PROCESS = "grievance:process"
    GRIEVANCE_ESCALATE = "grievance:escalate"
    BREACH_MANAGE = "breach:manage"
    RETENTION_MANAGE = "retention:manage"
    NOTIFICATION_MANAGE = "notification:manage"

    # Evidence — read and verify only. There is deliberately no write capability.
    AUDIT_READ = "audit:read"
    AUDIT_VERIFY = "audit:verify"
    REPORT_GENERATE = "report:generate"

    # Tenant administration
    USER_MANAGE = "user:manage"
    APIKEY_MANAGE = "apikey:manage"

    # Connections to a customer's own systems. Its own capability rather than
    # folding into apikey:manage, because the blast radius is different in kind:
    # an API key of ours grants access to us, while a connection holds the
    # customer's live Razorpay key, AWS credential or database password. Admin
    # only — deliberately not granted to the auditor, whose read-only remit does
    # not extend to a list of a company's production systems.
    CONNECTION_MANAGE = "connection:manage"
    TENANT_MANAGE = "tenant:manage"


# Every human is also a data subject.
#
# Staff have accounts, the company holds their data, and the DPDP Act does not
# stop applying to someone because they work there. Until this existed, a DPO
# could process everybody's rights requests and had no way to raise their own —
# which is both a compliance gap and an absurdity.
#
# Granting these to an auditor does not weaken "read-only by construction": the
# capabilities are scoped to *self*, so an auditor can exercise their own rights
# and still cannot touch anything they audit.
_SELF: frozenset[Capability] = frozenset({
    Capability.SELF_READ,
    Capability.SELF_CONSENT_WRITE,
    Capability.SELF_DSAR_WRITE,
    Capability.SELF_GRIEVANCE_WRITE,
})

_MATRIX: dict[Role, frozenset[Capability]] = {
    Role.DATA_PRINCIPAL: _SELF,
    Role.ADMIN: _SELF | frozenset({
        Capability.CONSENT_READ, Capability.CONSENT_VALIDATE, Capability.PURPOSE_MANAGE,
        Capability.DSAR_READ, Capability.DSAR_PROCESS,
        Capability.GRIEVANCE_READ, Capability.GRIEVANCE_PROCESS, Capability.GRIEVANCE_ESCALATE,
        Capability.BREACH_MANAGE, Capability.RETENTION_MANAGE, Capability.NOTIFICATION_MANAGE,
        Capability.AUDIT_READ, Capability.AUDIT_VERIFY, Capability.REPORT_GENERATE,
        Capability.USER_MANAGE, Capability.APIKEY_MANAGE, Capability.TENANT_MANAGE,
        Capability.CONNECTION_MANAGE,
    }),
    # Read-only by construction: an auditor who could change what they audit is
    # not an auditor.
    Role.AUDITOR: _SELF | frozenset({
        Capability.AUDIT_READ, Capability.AUDIT_VERIFY,
        Capability.REPORT_GENERATE, Capability.CONSENT_READ,
    }),
    Role.GRIEVANCE_OFFICER: _SELF | frozenset({
        Capability.GRIEVANCE_READ, Capability.GRIEVANCE_PROCESS,
        Capability.GRIEVANCE_ESCALATE,
    }),
}


def capabilities_for(role: Role | str) -> frozenset[Capability]:
    try:
        return _MATRIX[Role(role)]
    except ValueError:
        return frozenset()


def role_can(role: Role | str, capability: Capability) -> bool:
    return capability in capabilities_for(role)


# --------------------------------------------------------------------------
# API-key scopes — separate vocabulary from human roles, deliberately.
#
# A key embedded in a customer's marketing service should be able to ask "do I
# have consent?" and nothing else. Reusing human roles here would eventually give
# some integration the ability to erase a person.
# --------------------------------------------------------------------------
class Scope(StrEnum):
    CONSENT_READ = "consent:read"

    # Collect and withdraw are SEPARATE scopes, and the split is the point.
    #
    # A single `consent:write` let one credential both record and destroy consent.
    # That is tolerable for a secret server-side key and unacceptable for a
    # publishable key sitting in a browser bundle: forging a consent is bad, but
    # withdrawing a real one is worse — it deletes genuine evidence and triggers
    # the customer's downstream processing stops for a person who never asked.
    #
    # Publishable keys therefore get CONSENT_COLLECT and nothing else.
    CONSENT_COLLECT = "consent:collect"
    CONSENT_WITHDRAW = "consent:withdraw"

    DSAR_WRITE = "dsar:write"
    PRINCIPAL_READ = "principal:read"


ALL_SCOPES = frozenset(Scope)

# What a publishable key may ever hold. Not a default that can be widened at the
# call site — a ceiling, enforced when the key is created, so no amount of
# console misconfiguration can put a withdraw capability in a browser bundle.
PUBLISHABLE_SCOPES = frozenset({Scope.CONSENT_COLLECT})


def validate_scopes(requested: list[str]) -> list[Scope]:
    out: list[Scope] = []
    for s in requested:
        try:
            out.append(Scope(s))
        except ValueError as exc:
            raise ValueError(f"unknown scope: {s}") from exc
    if not out:
        raise ValueError("an API key must have at least one scope")
    return out
