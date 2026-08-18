// ============================================================================
// Users, invitations and sessions.
//
// The one thing worth knowing before using this: **there is no way to set
// somebody else's password.** An administrator who knows a colleague's password
// makes every audit entry attributed to that colleague arguable, and the audit
// chain is this product's central claim. So people are invited, and they choose
// their own password when they accept.
//
// `acceptInvitation` is re-exported from ./auth.js rather than implemented here.
// It creates a session, so it needs the same cookie handling as sign-in.
// ============================================================================
import { apiFetch } from "./auth";

export const ROLE_LABELS = {
  admin: "Admin / DPO",
  auditor: "Auditor",
  grievance_officer: "Grievance Officer",
  data_principal: "Data Principal",
};

/** What each role is FOR, in a sentence. The matrix comes from the API. */
export const ROLE_BLURBS = {
  admin: "Runs the workspace. The only role that can manage users and API keys.",
  auditor:
    "Read-only by construction. Can generate every report and verify the audit " +
    "chain, and cannot change anything they audit.",
  grievance_officer:
    "Handles complaints and nothing else — not consent, not data requests, not " +
    "the audit trail.",
  data_principal:
    "A person whose data you hold. Sees only their own records and exercises " +
    "only their own rights.",
};

// --------------------------------------------------------------------------
// Users
// --------------------------------------------------------------------------

export function listUsers() {
  return apiFetch("/admin/users");
}

/**
 * Change a role.
 *
 * A demotion signs them out everywhere — the server revokes their refresh-token
 * families, because the role is re-read per request but a refresh token outlives
 * a demotion. Say so before the click.
 */
export function changeRole(userId, role) {
  return apiFetch(`/admin/users/${userId}/role`, {
    method: "PATCH",
    body: { role },
  });
}

/** Revoke access. Also ends every live session. */
export function deactivateUser(userId) {
  return apiFetch(`/admin/users/${userId}/deactivate`, { method: "POST" });
}

// --------------------------------------------------------------------------
// Invitations
// --------------------------------------------------------------------------

export function listInvitations({ pendingOnly = false } = {}) {
  return apiFetch(`/admin/invitations?pending_only=${pendingOnly}`);
}

/**
 * Invite somebody. The response carries the link ONCE.
 *
 * It is also emailed, but the default notification provider writes to a log
 * instead of sending — so the console shows the link. Do not store it anywhere:
 * if it is lost, revoke the invitation and send a new one.
 */
export function invite({ email, role }) {
  return apiFetch("/admin/invitations", { method: "POST", body: { email, role } });
}

export function revokeInvitation(id, reason) {
  return apiFetch(`/admin/invitations/${id}/revoke`, {
    method: "POST",
    body: { reason: reason || "withdrawn by an administrator" },
  });
}

// --------------------------------------------------------------------------
// Sessions
// --------------------------------------------------------------------------

/** One entry per refresh-token family — that is, per browser. */
export function listSessions(userId) {
  return apiFetch(`/admin/users/${userId}/sessions`);
}

/**
 * Sign a user out everywhere.
 *
 * Their password still works: this ends the sessions, it does not lock the
 * account. Deactivation is the lockout.
 */
export function revokeSessions(userId) {
  return apiFetch(`/admin/users/${userId}/sessions/revoke`, { method: "POST" });
}

// --------------------------------------------------------------------------
// The capability matrix
// --------------------------------------------------------------------------

/**
 * What each role may do, generated from the code that enforces it.
 *
 * Never hardcode this in the frontend. A permissions screen that can disagree
 * with the enforcement tells an administrator their workspace is configured one
 * way while it behaves another — which is worse than showing nothing.
 */
export function capabilities() {
  return apiFetch("/admin/capabilities");
}

// --------------------------------------------------------------------------
// Accepting
// --------------------------------------------------------------------------
//
// `acceptInvitation` lives in ./auth.js, not here. It creates a session, so it
// needs the same cookie handling and the same session-application path as
// sign-in — a second implementation would be the place the refresh cookie
// silently stopped being set.
export { acceptInvitation } from "./auth";

// --------------------------------------------------------------------------
// Scheduled jobs
// --------------------------------------------------------------------------

/**
 * Is the background scheduler running, and what has it done?
 *
 * Worth surfacing prominently rather than burying on a settings page. Four
 * modules previously said "there is no scheduler", which was honest and visible.
 * A scheduler that stopped weeks ago is worse: escalation, notification retries
 * and pre-purge warnings all quietly stop while every screen implies they are
 * automatic.
 *
 * `stale` is the field that matters. Requires TENANT_MANAGE — this is deployment
 * health, not one workspace's compliance.
 */
export function jobs() {
  return apiFetch("/admin/jobs");
}

export function runJob(name) {
  return apiFetch(`/admin/jobs/${name}/run`, { method: "POST" });
}
