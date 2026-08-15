# Build brief — `users` (invitations & role management)

> Paste this whole file as the opening prompt. Fill the `[DECIDE]` block first.

**Size: 2–3 days.** The smallest remaining module, and it has no dependencies —
build it whenever there is a gap. It does touch authentication, so the bar is
higher than the size suggests.

---

## 1. What exists

Most of this module is **already built and tested** — it is the *screen* that is
mock.

Existing and working (`app/api/v1/admin.py`):
```
GET   /v1/admin/users                    list
POST  /v1/admin/users                    create with a password
PATCH /v1/admin/users/{id}/role          change role
POST  /v1/admin/users/{id}/deactivate    revoke access
```
Backed by `tenant_service`, capability-guarded with `USER_MANAGE`, audited via
`USER_CREATED` / `USER_ROLE_CHANGED` / `USER_DEACTIVATED`. The role is re-read
from the database on **every** request, so a revoked role stops working
immediately rather than when a token expires.

`/admin/roles` (`UserRoleManagement.jsx`) renders `MOCK_USERS_ADMIN` with locked
buttons.

## 2. The gap

1. The screen does not call the real API.
2. **Creating a user means typing a password for them.** An admin who knows a
   colleague's password is a bad default: it defeats non-repudiation, and every
   audit entry by that user is arguable. Real products invite.
3. No way to see or manage a user's sessions when something goes wrong.

## 3. `[DECIDE]`

- `[DECIDE]` **Invitation flow or admin-set password?** An invite needs working
  email (`03-notifications.md`).
  **Recommendation: invitations, and build this after notifications** — or build
  it now and have the invite link displayed once in the console until email
  exists. Do **not** ship admin-set passwords as the permanent design.

- `[DECIDE]` **Can the last admin be deactivated or demoted?**
  **Recommendation: no** — refuse it in the service *and* enforce it in the
  database. A workspace with no admin is unrecoverable without support access,
  which is the worst possible support ticket.

- `[DECIDE]` **Does a role change kill existing sessions?** The role is re-read
  per request so a *demotion* takes effect immediately. Refresh tokens, though,
  outlive it.
  **Recommendation: a demotion or deactivation revokes the user's refresh-token
  family**, reusing the machinery that already exists for reuse detection.

## 4. Data model

Mostly none — `users` exists. Add:

```
user_invitations
  tenant_id, email, role
  token_hash              Argon2, like refresh tokens. Never the raw token.
  invited_by -> users(id)
  expires_at              short: 72 hours
  accepted_at, revoked_at
  UNIQUE (tenant_id, email) WHERE accepted_at IS NULL AND revoked_at IS NULL
```

**The invitation token is a credential.** It grants the ability to create an
account with a chosen role in someone else's workspace. Treat it exactly like a
refresh token:

- Argon2-hashed at rest, raw value shown once.
- Short expiry.
- Single use — accepting revokes it.
- **The tenant travels in the token** (`<tenant-hex>.<secret>`), because the
  acceptance lookup happens before any tenant context exists and
  `user_invitations` will be under RLS. This codebase has hit that bug three
  times; do not make it four.

## 5. API

```
GET    /v1/admin/invitations                 pending (USER_MANAGE)
POST   /v1/admin/invitations                 invite: email + role
POST   /v1/admin/invitations/{id}/revoke
POST   /v1/auth/accept-invitation            PUBLIC — token + name + password
GET    /v1/admin/users/{id}/sessions         active refresh-token families
POST   /v1/admin/users/{id}/sessions/revoke  sign them out everywhere
```

`accept-invitation` is unauthenticated and creates a user, so it needs the same
care as `register`:

- Reuse `validate_password` from `registration_service` — one password policy,
  not two.
- Rate limit it.
- **Do not reveal whether an email already has an account** in the failure
  message.
- Accepting must be a single transaction: create the user, mark the invitation
  accepted, write both audit entries.

## 6. Roles

Four exist and are already enforced: `admin` (DPO), `auditor`,
`grievance_officer`, `data_principal`. The capability matrix lives in
`app/core/permissions.py` and `AUDIT_WRITE` / `AUDIT_DELETE` are **deliberately
absent from the enum entirely**, so no role can be misconfigured into holding
them. Keep it that way.

The screen should **show the actual capability matrix**, read from the backend
rather than duplicated in the frontend — a permissions screen that can disagree
with the enforcement is worse than none.

## 7. Frontend

- Real user list with role, status, last login, MFA state.
- Invite by email + role; show the invite link once if email is not yet wired.
- Pending invitations with expiry and a revoke action.
- Role change and deactivation behind `ConfirmModal` with the consequence stated
  ("they will be signed out of all devices").
- The capability matrix, from the API.
- The last-admin rule must surface as a clear message, not a generic 400.

## 8. Non-goals

- No SSO/OIDC — that is Phase 10 hardening (`external_idp` is already reserved on
  the user model).
- No custom roles or per-user capability overrides. Four roles is the product.
- No MFA enrolment flow (`require_mfa` exists on the tenant; enrolment is
  separate work).

## 9. Tests

- an invitation token is stored hashed and never returned twice
- an expired or already-accepted invitation is refused
- accepting creates exactly one user with exactly the invited role
- the invited role cannot be escalated by tampering with the acceptance payload
- the last admin cannot be demoted or deactivated — service **and** database
- a demotion revokes the user's refresh-token family
- a demoted user's next request is refused immediately, without waiting for token
  expiry
- `accept-invitation` does not reveal whether an email is already registered
- cross-tenant: an invitation from tenant A cannot create a user in tenant B,
  including with a token rewritten to claim B
- a non-`USER_MANAGE` role gets 403 on every admin route here

## 10. Definition of done

- [ ] Invite → accept → sign in → change role → deactivate, end to end
- [ ] No admin-set passwords remaining
- [ ] Capability matrix rendered from the backend
- [ ] `users` flipped to `"live"` in `src/config/modules.js`
- [ ] If email is not wired yet, the one-time invite link is clearly labelled as
      a stopgap in `MODULE_CAVEATS`
- [ ] `./scripts/acceptance.sh http://localhost:8090` still passes

## 11. House rules

RLS + `TENANT_SCOPED_TABLES` + policy test. Timezone-aware timestamps. Tenant
travels in any credential looked up before context exists. Audit every state
change. Full list: [README.md](README.md).
