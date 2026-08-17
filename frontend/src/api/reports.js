// ============================================================================
// Compliance reports.
//
// Two things this client deliberately does not have:
//
//   * **A PDF format.** The server produces CSV and JSON. A PDF option that
//     returned "coming soon" would be worse than its absence — it makes a
//     customer plan around a capability that does not exist.
//   * **Anything called "signed".** Every report carries the audit chain head
//     hash, which the reader can recompute and check against
//     POST /v1/audit/verify. That is tamper evidence, not a signature, and the
//     provenance block says so in those words.
//
// The provenance block comes back pre-rendered as `provenance_lines`, identical
// to what the export carries. The screen shows it verbatim, because the person
// reading the page is making the same decisions as the person reading the file —
// and a caveat that only appears in the download is one most people never see.
// ============================================================================
import { apiDownload, apiFetch } from "./auth";

export const FORMATS = [
  { id: "csv", label: "CSV", hint: "Opens in a spreadsheet." },
  { id: "json", label: "JSON", hint: "For a downstream system." },
];

/** Ranges offered on screen, resolved to real dates here rather than on the server. */
export const RANGES = [
  { id: "7d", label: "Last 7 days", days: 7 },
  { id: "30d", label: "Last 30 days", days: 30 },
  { id: "90d", label: "Last 90 days", days: 90 },
  { id: "365d", label: "Last 12 months", days: 365 },
];

/**
 * A range id to `{date_from, date_to}`.
 *
 * `date_to` is today and the server treats it as inclusive, so "last 7 days"
 * covers all of today rather than stopping at midnight this morning.
 */
export function rangeToDates(rangeId) {
  const range = RANGES.find((r) => r.id === rangeId) || RANGES[1];
  const to = new Date();
  const from = new Date(to.getTime() - range.days * 864e5);
  const iso = (d) => d.toISOString().slice(0, 10);
  return { date_from: iso(from), date_to: iso(to) };
}

/** The report types this workspace can generate, and the server's limits. */
export function catalogue() {
  return apiFetch("/reports");
}

/**
 * A page of a report, with its provenance block.
 *
 * `verifyChain` is off by default. Verifying walks every entry in the tenant's
 * audit chain, so it is a deliberate action — and left off, the block honestly
 * reports "not_checked" rather than implying a check nobody ran.
 */
export function preview(
  reportKey,
  { rangeId = "30d", offset = 0, limit = 50, verifyChain = false } = {},
) {
  const { date_from, date_to } = rangeToDates(rangeId);
  const q = new URLSearchParams({
    date_from,
    date_to,
    offset: String(offset),
    limit: String(limit),
    verify_chain: String(Boolean(verifyChain)),
  });
  return apiFetch(`/reports/${reportKey}/preview?${q}`);
}

/**
 * Generate and hand the file to the browser.
 *
 * Streamed from the server and never stored there — nothing is written to disk
 * or to a report table, because a stored report is a snapshot that can disagree
 * with the data it came from.
 */
export async function download(
  reportKey,
  { rangeId = "30d", format = "csv", verifyChain = false } = {},
) {
  const { date_from, date_to } = rangeToDates(rangeId);
  const { blob, filename } = await apiDownload(`/reports/${reportKey}/generate`, {
    method: "POST",
    body: { date_from, date_to, format, verify_chain: verifyChain },
  });

  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoked on the next tick rather than immediately: some browsers have not
  // finished reading the blob when click() returns.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return filename;
}
