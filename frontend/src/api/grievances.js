// ============================================================================
// Grievance redressal — DPDP §13.
//
// Three audiences, three sets of calls, and the separation is the security
// model rather than a tidiness preference:
//
//   * a data principal      → file, mine, officer, rate
//   * a DPO / officer       → the queue, triage, escalate
//   * anybody at all        → filePublicly, confirmPublicly (no credential)
//
// The public pair deliberately uses plain `fetch`, not `apiFetch`. A person
// filing a complaint has no session, and routing them through the authenticated
// client would attach credentials they do not have and retry on a 401 they will
// always get.
// ============================================================================
import { apiFetch } from "./auth";

/** Mirrors GRIEVANCE_CATEGORIES on the server. The server validates; this labels. */
export const CATEGORIES = [
  { id: "consent_violation", label: "Consent was ignored or misused" },
  { id: "dsar_delay", label: "A data request was not answered in time" },
  { id: "inaccurate_data", label: "My data is wrong and was not corrected" },
  { id: "data_breach", label: "My data was exposed or leaked" },
  { id: "other", label: "Something else" },
];

export const CATEGORY_LABEL = Object.fromEntries(
  CATEGORIES.map((c) => [c.id, c.label]),
);

export const STATUS_LABEL = {
  open: "Open",
  acknowledged: "Acknowledged",
  in_progress: "In progress",
  resolved: "Resolved",
  rejected: "Not upheld",
  reopened: "Reopened",
};

// --------------------------------------------------------------------------
// The person who complained
// --------------------------------------------------------------------------

export function fileGrievance({ category, description, relatedDsarId }) {
  return apiFetch("/grievances", {
    method: "POST",
    body: {
      category,
      description,
      related_dsar_id: relatedDsarId || null,
    },
  });
}

export function myGrievances() {
  return apiFetch("/grievances/mine");
}

/**
 * The published Grievance Officer.
 *
 * Readable by everyone in the workspace, data principals included — §13 requires
 * this contact to be *published*, and a person who has to ask an administrator
 * who to complain to has not been given a redressal mechanism.
 */
export function officer() {
  return apiFetch("/grievances/officer");
}

export function saveOfficer({ name, email, slaDays, escalationDays }) {
  return apiFetch("/grievances/officer", {
    method: "PUT",
    body: {
      name,
      email,
      sla_days: slaDays ?? null,
      escalation_days: escalationDays ?? null,
    },
  });
}

/**
 * Rate a resolution. 1 or 2 reopens the grievance.
 *
 * That is deliberate, and worth knowing before you call it: a satisfaction score
 * that feeds a dashboard and changes nothing is a metric, not redress.
 */
export function rateResolution(id, { rating, comment }) {
  return apiFetch(`/grievances/${id}/feedback`, {
    method: "POST",
    body: { rating, comment: comment || null },
  });
}

// --------------------------------------------------------------------------
// The queue
// --------------------------------------------------------------------------

export function listGrievances({
  status,
  category,
  assignedTo,
  escalatedOnly,
  overdueOnly,
  limit = 100,
} = {}) {
  const q = new URLSearchParams();
  if (status) q.set("status", status);
  if (category) q.set("category", category);
  if (assignedTo) q.set("assigned_to", assignedTo);
  if (escalatedOnly) q.set("escalated_only", "true");
  if (overdueOnly) q.set("overdue_only", "true");
  q.set("limit", String(limit));
  return apiFetch(`/grievances?${q}`);
}

export function getGrievance(id) {
  return apiFetch(`/grievances/${id}`);
}

/**
 * Move a grievance along.
 *
 * `resolved` requires `resolutionNotes` and `rejected` requires
 * `rejectionReason`; the server refuses otherwise and so does the database. Do
 * not paper over that in the UI — a resolution with no record of the redress is
 * the precise shape of a redressal mechanism that isn't one.
 */
export function changeStatus(id, { toStatus, resolutionNotes, rejectionReason, note }) {
  return apiFetch(`/grievances/${id}`, {
    method: "PATCH",
    body: {
      to_status: toStatus,
      resolution_notes: resolutionNotes || null,
      rejection_reason: rejectionReason || null,
      note: note || null,
    },
  });
}

export function assignGrievance(id, userId) {
  return apiFetch(`/grievances/${id}/assign`, {
    method: "POST",
    body: { user_id: userId || null },
  });
}

/**
 * Escalate to the Grievance Officer. Idempotent.
 *
 * Does NOT contact the Data Protection Board. Unattended regulator contact is a
 * decision a person makes; this flag is what tells them to make it.
 */
export function escalateGrievance(id, reason) {
  return apiFetch(`/grievances/${id}/escalate`, {
    method: "POST",
    body: { reason: reason || null },
  });
}

// --------------------------------------------------------------------------
// Public filing — no credential
// --------------------------------------------------------------------------

const PUBLIC_BASE =
  import.meta.env.VITE_PUBLIC_BASE || `${window.location.origin}/public/v1`;

async function publicPost(path, body) {
  const res = await fetch(`${PUBLIC_BASE}/grievance${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    throw new Error(data?.detail || `Request failed (${res.status})`);
  }
  return data;
}

/**
 * File without an account.
 *
 * The complaint is recorded immediately and the deadline starts running. It will
 * not escalate to the Grievance Officer until the address is confirmed — that is
 * the trade for not requiring a credential, and the returned message says so.
 */
export function filePublicly({ workspace, category, description, contactEmail }) {
  return publicPost("", {
    workspace,
    category,
    description,
    contact_email: contactEmail,
  });
}

export function confirmPublicly({ workspace, reference, token }) {
  return publicPost("/confirm", { workspace, reference, token });
}
