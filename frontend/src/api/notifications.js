// ============================================================================
// Notification templates and the delivery log.
//
// There is no `send()` here, and that is not an omission. Every message this
// product sends is triggered by a state change that carries an obligation — a
// request was received, a consent withdrawn, data is about to be purged. A
// "send arbitrary message" call would turn a compliance mailer into a general
// one, and the delivery log's worth as evidence rests on every row in it having
// a reason.
//
// `previewTemplate` renders with sample values instead, because somebody editing
// a statutory notification needs to see what it will say before ten thousand
// people do.
// ============================================================================
import { apiFetch } from "./auth";

/**
 * Which provider is configured, and whether it actually sends anything.
 *
 * The first thing the screen has to say. A page showing eight healthy templates
 * while the console provider is selected is telling a fiduciary they are
 * notifying people when nothing has left the building.
 */
export function getProvider() {
  return apiFetch("/notifications/provider");
}

export function listTemplates() {
  return apiFetch("/notifications/templates");
}

/** Placeholders are validated server-side here — an unknown one is refused. */
export function saveTemplate({ key, channel, language, subject, body }) {
  return apiFetch("/notifications/templates", {
    method: "PUT",
    body: { key, channel: channel || "email", language: language || "English", subject, body },
  });
}

/** Renders with `[placeholder]` samples. Sends nothing. */
export function previewTemplate({ key, channel, language, subject, body }) {
  return apiFetch("/notifications/templates/preview", {
    method: "POST",
    body: { key, channel: channel || "email", language: language || "English", subject, body },
  });
}

/**
 * The delivery log — delivered, failed AND suppressed.
 *
 * Suppressed rows are the interesting ones: "we never told them, because we hold
 * no address for them" is an answer a fiduciary needs to be able to give, and a
 * log filtered down to successes hides exactly what is worth looking at.
 */
export function deliveryLog({ status, principalId, limit = 100 } = {}) {
  const q = new URLSearchParams();
  if (status) q.set("status", status);
  if (principalId) q.set("principal_id", principalId);
  q.set("limit", String(limit));
  return apiFetch(`/notifications/log?${q}`);
}

/** Re-attempt one failed message. Does not reset the attempt count. */
export function retry(id) {
  return apiFetch(`/notifications/log/${id}/retry`, { method: "POST" });
}

/**
 * Run one pass of the worker loop by hand.
 *
 * Exists because no scheduler is deployed yet: without it a message that hit a
 * transient failure would sit in `queued` forever.
 */
export function drain() {
  return apiFetch("/notifications/drain", { method: "POST" });
}

/**
 * A data principal's own notification history.
 *
 * "You were informed on the 14th" is a claim made *about* somebody; the person
 * it is made about should be able to see it without having to ask. Scoped
 * server-side to the signed-in user, so there is no id to pass and none to
 * tamper with.
 */
export function myNotifications() {
  return apiFetch("/notifications/mine");
}
