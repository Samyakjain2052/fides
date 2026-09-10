// ============================================================================
// The vendor register — processors, their contracts, their documents.
//
// §8(2) is why it exists: a Data Fiduciary stays responsible for processing
// carried out by its processors, so every vendor holding personal data is part
// of the company's own compliance surface.
//
// THERE IS NO SCORE, AND THAT IS DELIBERATE.
//
// Competing products publish a number per vendor derived from litigation feeds,
// breach databases and diffed privacy policies. That number is the output of a
// research operation with people employed to maintain it — not a feature — and
// computing one from what a customer typed into a form would invent an
// authority we do not have. What comes back instead is `concerns`: a list of
// individually actionable facts, each with a `title`, a `severity` and a `why`
// explaining what turns on it.
//
// Concerns are computed server-side because one of them compares the vendor's
// rights-request turnaround against the tenant's own statutory deadline, and
// that deadline is server-side policy.
// ============================================================================
import { apiFetch, apiUpload } from "./auth";

export function vendors() {
  return apiFetch("/vendors");
}

export function vendor(id) {
  return apiFetch(`/vendors/${id}`);
}

export function createVendor({ name, domain, description, role, riskTier, ownerUserId }) {
  return apiFetch("/vendors", {
    method: "POST",
    body: {
      name,
      domain: domain || null,
      description: description || null,
      role: role || "processor",
      risk_tier: riskTier || "medium",
      owner_user_id: ownerUserId || null,
    },
  });
}

/**
 * Edit the register entry.
 *
 * Send only the keys you mean to change: the server treats an omitted field as
 * unchanged, and `status` is refused here on purpose — see `decideVendor`.
 */
export function updateVendor(id, patch) {
  return apiFetch(`/vendors/${id}`, { method: "PATCH", body: patch });
}

/**
 * Approve, approve with conditions, refuse, or retire.
 *
 * Separate from `updateVendor` because it is a decision about whether a third
 * party may receive personal data, with a reason and an audit entry. A refusal
 * and a conditional approval both require the note — in the second case the
 * note IS the record of what remains outstanding.
 */
export function decideVendor(id, { status, note }) {
  return apiFetch(`/vendors/${id}/decision`, {
    method: "POST",
    body: { status, note: note || null },
  });
}

export function addDocument(id, { kind, title, url, note }) {
  return apiFetch(`/vendors/${id}/documents`, {
    method: "POST",
    body: { kind, title, url: url || null, note: note || null },
  });
}

export function uploadDocument(id, file, kind = "other") {
  return apiUpload(`/vendors/${id}/documents/upload?kind=${kind}`, file);
}

/** Record that somebody read it, and what it said. */
export function reviewDocument(vendorId, documentId, content) {
  return apiFetch(`/vendors/${vendorId}/documents/${documentId}/review`, {
    method: "POST",
    body: { content: content || null },
  });
}

/**
 * Compare fresh text against what was last read.
 *
 * Takes the content rather than a URL. Fetching an arbitrary customer-supplied
 * URL from the server is an SSRF primitive, and this codebase has already
 * learned that lesson once — see connectors/hosts.py.
 */
export function checkDocument(vendorId, documentId, content) {
  return apiFetch(`/vendors/${vendorId}/documents/${documentId}/check`, {
    method: "POST",
    body: { content },
  });
}

export function linkSystem(id, connectionId) {
  return apiFetch(`/vendors/${id}/systems`, {
    method: "POST",
    body: { connection_id: connectionId },
  });
}

export function unlinkSystem(id, connectionId) {
  return apiFetch(`/vendors/${id}/systems/${connectionId}`, {
    method: "DELETE",
  });
}

export const STATUS_LABEL = {
  prospective: "Under review",
  approved: "Approved",
  conditional: "Approved with conditions",
  rejected: "Refused",
  retired: "Retired",
};

export const STATUS_TONE = {
  prospective: "neutral",
  approved: "success",
  conditional: "warning",
  rejected: "danger",
  retired: "neutral",
};

export const ROLE_LABEL = {
  processor: "Processor",
  sub_processor: "Sub-processor",
  joint: "Joint fiduciary",
  recipient: "Independent recipient",
};

export const TIER_LABEL = {
  low: "Low",
  medium: "Medium",
  high: "High",
  critical: "Critical",
};

export const DOCUMENT_KINDS = [
  ["privacy_policy", "Privacy policy"],
  ["dpa", "Data processing agreement"],
  ["subprocessor_list", "Sub-processor list"],
  ["certification", "Certification"],
  ["security_report", "Security report"],
  ["breach_notice", "Breach notice"],
  ["other", "Other"],
];

export const DATA_CATEGORIES = [
  "Name and contact details",
  "Government identifiers",
  "Financial data",
  "Health data",
  "Biometric data",
  "Location data",
  "Behavioural or usage data",
  "Employment or education records",
];

export const CERTIFICATIONS = [
  "ISO 27001",
  "ISO 27701",
  "SOC 2 Type II",
  "PCI DSS",
  "HIPAA",
];
