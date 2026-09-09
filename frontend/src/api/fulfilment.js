// ============================================================================
// Answering a rights request: identity, correspondence, disclosure.
//
// Separate from ./dsar.js, which is about the request as a record — raise it,
// track it, move its status. This is about discharging the obligation, and the
// two have different audiences: everything here is either staff-only or
// subject-only, and almost nothing is both.
//
// WHAT THE BROWSER IS NOT TRUSTED WITH
//
// Redaction happens on the server. `preview` returns values already replaced
// with <SENSITIVE VALUE EXCLUDED>, so a DPO with the network tab open sees
// exactly what the screen shows. Sending the real values down and hiding them
// in CSS would be a redaction anybody could defeat with devtools.
//
// The direction of a message is also the server's decision, derived from who is
// authenticated. A browser that could set it would be able to forge a message
// appearing to come from the data principal — in a statutory correspondence
// record, that is evidence fabrication.
// ============================================================================
import { apiDownload, apiFetch, apiUpload } from "./auth";

// ---------------------------------------------------------------- identity --

/** Attach identity proof. Your own request, or anyone's with dsar:process. */
export function uploadIdentityDocument(requestId, file) {
  return apiUpload(`/dsar/${requestId}/identity`, file);
}

/**
 * Open the submitted document. Needs dsar:process, and every look is recorded.
 *
 * Deliberately not rendered inline anywhere. The server sends it as an
 * attachment with a sandbox CSP, and an <img src> pointed at it would both
 * defeat that and put a copy in the browser cache.
 */
export function downloadIdentityDocument(requestId) {
  return apiDownload(`/dsar/${requestId}/identity/document`, { method: "GET" });
}

/** Accept or refuse. A refusal needs a reason; the server enforces it too. */
export function reviewIdentity(requestId, { accept, reason }) {
  return apiFetch(`/dsar/${requestId}/identity/review`, {
    method: "POST",
    body: { accept, reason },
  });
}

// --------------------------------------------------------------- messages --

/** Both sides of the thread. Reading marks the other side's messages seen. */
export function messages(requestId) {
  return apiFetch(`/dsar/${requestId}/messages`);
}

/** Post one. Direction comes from who you are, never from here. */
export function sendMessage(requestId, body) {
  return apiFetch(`/dsar/${requestId}/messages`, {
    method: "POST",
    body: { body },
  });
}

// ------------------------------------------------------------- disclosure --

/**
 * What would be disclosed, with sensitive values already excluded server-side.
 *
 * This is the only view of the disclosure staff get. The assembled package is
 * readable by its subject and by nobody else — see services/disclosure.py for
 * why that is a deliberate limit rather than an oversight.
 */
export function previewDisclosure(requestId) {
  return apiFetch(`/dsar/${requestId}/disclosure/preview`);
}

/**
 * Build and store the package. Does NOT send it.
 *
 * `confirmReference` must be the request's own reference, typed back — the same
 * guard the retention live run and the connected erasure use. This gathers one
 * person's entire record into a single object.
 */
export function assembleDisclosure(requestId, confirmReference) {
  return apiFetch(`/dsar/${requestId}/disclosure/assemble`, {
    method: "POST",
    body: { confirm_reference: confirmReference },
  });
}

/** Put the assembled package in the thread and notify the requester. */
export function deliverDisclosure(requestId, coveringNote) {
  return apiFetch(`/dsar/${requestId}/disclosure/deliver`, {
    method: "POST",
    body: { covering_note: coveringNote || null },
  });
}

/** The subject collecting their own package. Audited server-side. */
export function downloadPackage(requestId) {
  return apiDownload(`/dsar/${requestId}/package`, { method: "GET" });
}

/** Any stored file the caller is allowed to read, by id. */
export function downloadFile(fileId) {
  return apiDownload(`/files/${fileId}`, { method: "GET" });
}

/**
 * Hand a Blob to the browser as a save.
 *
 * One place, because getting it wrong leaks: an object URL that is never
 * revoked keeps the bytes alive in the page for as long as the document lives,
 * and these bytes are somebody's complete personal record.
 */
export function saveBlob({ blob, filename }, fallbackName) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || fallbackName || "download";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** Bytes, for a human. */
export function humanSize(bytes) {
  if (!bytes) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Categories the preview will have excluded, for explaining the marker. */
export const EXCLUDED_MARKER = "<SENSITIVE VALUE EXCLUDED>";
export const WITHHELD_MARKER = "<WITHHELD — CREDENTIAL MATERIAL>";
