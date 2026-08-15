// ============================================================================
// Rights requests, from the server.
//
// This replaces the localStorage stopgap that used to live in ./consent.js. That
// existed because the gateway had no list-by-identity endpoint, so the browser
// had to remember the ids it created — which meant a request was invisible to
// the DPO, invisible on another device, and gone if the person cleared their
// browser, while the erasure it triggered had genuinely happened.
//
// The record now lives in PostgreSQL. Nothing here persists anything locally,
// and that is the point.
// ============================================================================
import { apiFetch } from "./auth";

/** Raise a request. Omit principalId to raise your own. */
export function submitRequest({ type, verificationMethod, correctionPayload, principalId }) {
  return apiFetch("/dsar", {
    method: "POST",
    body: {
      type,
      verification_method: verificationMethod,
      correction_payload: correctionPayload,
      // Only sent when acting for someone else, which needs dsar:process. The
      // server records that it was staff-initiated either way.
      ...(principalId ? { principal_id: principalId } : {}),
    },
  });
}

/** The signed-in person's own requests. */
export function myRequests() {
  return apiFetch("/dsar/mine");
}

/** The fiduciary triage queue. */
export function queue({ status, type, overdueOnly } = {}) {
  const q = new URLSearchParams();
  if (status) q.set("status", status);
  if (type) q.set("type", type);
  if (overdueOnly) q.set("overdue_only", "true");
  return apiFetch(`/dsar?${q}`);
}

export function getRequest(id) {
  return apiFetch(`/dsar/${id}`);
}

/**
 * Advance, reject or cancel.
 *
 * `reason` is required when rejecting — the server and the database both refuse
 * a rejection without one, because a rejection with no recorded reason is not a
 * decision anyone can defend.
 */
export function changeStatus(id, { toStatus, reason, note }) {
  return apiFetch(`/dsar/${id}/status`, {
    method: "PATCH",
    body: { to_status: toStatus, reason, note },
  });
}

/** Re-dispatch after a failed engine call. The request was never lost. */
export function retryDispatch(id) {
  return apiFetch(`/dsar/${id}/retry`, { method: "POST" });
}

/**
 * The access package: one person's complete personal data.
 *
 * Every retrieval is audited server-side, and the package expires — an expired
 * one says so rather than 404ing, because the person is entitled to know it
 * existed and that the window closed.
 */
export function getPackage(id) {
  return apiFetch(`/dsar/${id}/package`);
}

/** Download the package as a file, without ever putting it in a URL. */
export async function downloadPackage(id, reference) {
  const data = await getPackage(id);
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `access-package-${reference || id}.json`;
  a.click();
  URL.revokeObjectURL(url);
  return data;
}

/**
 * Server shape -> the shape the existing screens render.
 *
 * A thin adapter rather than a rewrite of three screens: the fields they show
 * are the same facts, under different names. Extra server fields (the timeline,
 * the allowed transitions, overdue) are passed through so the screens can start
 * using them without another round of plumbing.
 */
export function toRow(d) {
  return {
    id: d.id,
    reference: d.reference,
    type: d.type,
    status: d.status,
    submitted_at: d.submitted_at,
    deadline_at: d.deadline_at,
    resolved_at: d.resolved_at,
    rejection_reason: d.rejection_reason,
    verification: d.verification_method,
    user_email: d.principal_email,
    user_id: d.principal_ref,
    correction: d.correction_payload,
    // An access package is only offered when the server says there is one and
    // the window is still open. Rendering a download that 409s would be worse
    // than not rendering it.
    export_url:
      d.type === "access" && d.status === "completed" && d.package_available_until
        ? `/v1/dsar/${d.id}/package`
        : null,
    package_available_until: d.package_available_until,
    engine_ref: d.engine_ref,
    engine_status: d.engine_status,
    engine_error: d.engine_error,
    requested_by_actor: d.requested_by_actor,
    timeline: d.timeline || [],
    allowed_transitions: d.allowed_transitions || [],
    overdue: d.overdue,
    days_remaining: d.days_remaining,
  };
}

export async function myRows() {
  return (await myRequests()).map(toRow);
}

export async function queueRows(filters) {
  const page = await queue(filters);
  return { rows: page.items.map(toRow), total: page.total };
}
