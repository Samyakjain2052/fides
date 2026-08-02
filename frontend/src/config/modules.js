// ============================================================================
// Module status — the single source of truth for what this product actually
// does today.
//
// Five of the seven screen groups render sample data. Showing those to a buyer
// without saying so has two outcomes: they don't notice and we have mis-sold, or
// they do notice and the trust is gone. For a *compliance* product the second is
// fatal, so every claim in the UI is derived from this file rather than written
// by hand on each screen.
//
// Changing a module to "live" is a deliberate act. Do not flip one because a
// screen looks finished — flip it when the data behind it is real.
// ============================================================================

/** Where the preview modules are heading. One place, so it cannot drift. */
export const SHIP_TARGET = "Q4 2026";

/**
 * "live"    — backed by a real service and real data.
 * "preview" — the screen is built, the data is sample. Interactions are disabled.
 */
export const MODULE_STATUS = {
  // Real: tenants, users, argon2 passwords, JWT + rotating refresh tokens, RLS
  // isolation, and the append-only audit chain, all in PostgreSQL.
  auth: "live",

  // Real: a Data Principal submits an access or erasure request and it executes
  // against the Fides engine in this repo, fanning out to four datastores.
  // Correction and identity verification are the exceptions — see MODULE_CAVEATS,
  // because a bare "DSAR is live" would overclaim.
  dsar: "live",

  // Separate key on purpose. Submitting and tracking a request is real; the
  // fiduciary-side triage queue (approve, reject, reassign, prepare an export)
  // has no backend behind it. One "dsar: live" covering both would be false on
  // the half a buyer's DPO spends the most time in.
  dsar_workflow: "preview",

  consent: "preview",
  grievance: "preview",
  retention: "preview",
  reports: "preview",
  audit: "preview",
  breach: "preview",
  notifications: "preview",
  users: "preview",
};

export const MODULE_LABELS = {
  auth: "Accounts & sign-in",
  dsar: "Data requests (DSAR)",
  dsar_workflow: "DSAR triage queue",
  consent: "Consent management",
  grievance: "Grievance redressal",
  retention: "Retention & purge",
  reports: "Reports",
  audit: "Audit trail",
  breach: "Breach management",
  notifications: "Notifications",
  users: "Users & roles",
};

/**
 * Where a module is live but not *entirely* live. Surfaced on the screen and on
 * the roadmap. A caveat is not a footnote — an unstated exception is the same
 * mis-sell the preview banner exists to prevent.
 */
export const MODULE_CAVEATS = {
  dsar:
    "Access and erasure run for real against four datastores. Two parts are not: " +
    "correction is sample data (the engine has no correction action yet), and " +
    "the OTP / DigiLocker identity check is simulated, not a real verification.",
};

/** What each preview module needs before it can be called live. */
export const MODULE_ROADMAP = {
  consent: "Purposes, versioned notices, and the consent lifecycle bound to a notice version.",
  grievance: "Grievance intake, officer assignment, and the statutory escalation clock.",
  retention: "Retention policies with real purge execution and exemption handling.",
  reports: "Reports generated from real consent and request data, exportable.",
  audit: "The backend already keeps a tamper-evident hash-chained audit trail; this screen is not yet reading it.",
  breach: "Breach register with Board and Data Principal notification workflows.",
  notifications: "Email and SMS delivery with per-tenant templates.",
  users: "Server-side user invitation and role assignment.",
  dsar_workflow: "Fiduciary-side triage: approve, reject, reassign, and prepare an export. The request execution behind it is already live.",
};

/**
 * Status for a module key.
 *
 * Unknown keys resolve to "preview" on purpose. A typo must fail toward telling
 * the truth: silently claiming a screen is live is the one error mode this file
 * exists to prevent.
 */
export function statusOf(key) {
  return MODULE_STATUS[key] === "live" ? "live" : "preview";
}

export function isPreview(key) {
  return statusOf(key) !== "live";
}

export function previewModules() {
  return Object.keys(MODULE_STATUS).filter(isPreview);
}

export function liveModules() {
  return Object.keys(MODULE_STATUS).filter((k) => !isPreview(k));
}

/**
 * Props for a control that must not act in a preview module.
 *
 *   <button {...previewLock("consent", "Saving your preferences")}>Save</button>
 *
 * Returns `{}` when the module is live, so the same call site works either way
 * and nothing has to be un-done when a module ships.
 *
 * `disabled` rather than a no-op handler: a button that looks active and does
 * nothing reads as a bug, and a buyer cannot tell a deliberate placeholder from
 * a broken product.
 */
export function previewLock(key, action = "This action") {
  if (!isPreview(key)) return {};
  return {
    disabled: true,
    "aria-disabled": "true",
    title: `${action} is not available in the preview. Shipping ${SHIP_TARGET}.`,
  };
}

/**
 * Route -> module. Longest prefix wins.
 *
 * The banner is rendered once, by the layout, from this map — not pasted into
 * each page. Eighteen page files each remembering to mount a banner is eighteen
 * chances to forget, and the one that forgets is the one that mis-sells.
 * A new screen inherits the right banner by living under the right path.
 */
const PATH_MODULES = [
  ["/admin/consent-validation", "consent"],
  ["/admin/notifications", "notifications"],
  ["/admin/grievances", "grievance"],
  ["/admin/retention", "retention"],
  ["/admin/breaches", "breach"],
  ["/admin/reports", "reports"],
  ["/admin/audit", "audit"],
  ["/admin/roles", "users"],
  ["/admin/dsar", "dsar_workflow"],
  ["/user/consent-history", "consent"],
  ["/user/preferences", "consent"],
  ["/user/grievance", "grievance"],
  ["/user/dsar", "dsar"],
  ["/consent-banner", "consent"],
  ["/cookie-consent", "consent"],
];

export function moduleForPath(pathname) {
  const hit = PATH_MODULES.find(([prefix]) => (pathname || "").startsWith(prefix));
  return hit ? hit[1] : null;
}
