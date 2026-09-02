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
  // Real: requests are rows in PostgreSQL with a server-computed statutory
  // deadline, an append-only timeline, and triage that the engine cannot
  // overrule. The localStorage stopgap this replaced is gone.
  dsar_workflow: "live",

  // Real as of Phase 3: purposes, versioned notices, data principals and the
  // consent lifecycle are in PostgreSQL, every change writes to the audit chain,
  // and a published notice is immutable at the database level.
  consent: "live",

  // The public consent banner and cookie banner are a separate key, and still
  // preview. They are unauthenticated screens shown to a first-time visitor, so
  // they cannot use the authenticated consent API that /user/preferences now
  // uses — collecting consent from an anonymous visitor needs the public API
  // (Phase 4). Flipping `consent` to live without this split would have silently
  // re-enabled two screens that still write to in-memory mock state.
  // Real: the banner and cookie surfaces collect through the publishable-key
  // API, with server-stamped provenance on every record.
  consent_surfaces: "live",

  // Still preview, and split out on purpose. DPDP §9 requires *verifiable*
  // parental consent, and a publishable key cannot verify a guardian — the OTP
  // and DigiLocker steps in that flow are simulated. Flipping consent_surfaces
  // live without this split would have silently unlocked a flow that still
  // writes to the in-memory mock.
  consent_guardian: "preview",
  grievance: "live",
  // Real: policies, a dry run that provably changes nothing, a live run that
  // demands the policy name back, and append-only receipts that record every
  // skip with its reason.
  retention: "live",

  // Editing an existing policy is not built — the API creates and runs, it does
  // not update. Split out so the control stays disabled rather than becoming a
  // button that errors when retention went live.
  reports: "live",
  // Real: the screen reads the HMAC hash-chained trail in PostgreSQL, and
  // "Verify chain integrity" walks it and reports the first break. The backend
  // chain was already real from Phase 2 — this only connected the screen to it.
  audit: "live",
  breach: "live",
  notifications: "live",
  users: "live",
  connections: "live",
};

export const MODULE_LABELS = {
  auth: "Accounts & sign-in",
  dsar: "Data requests (DSAR)",
  dsar_workflow: "DSAR triage queue",
  consent: "Consent management",
  consent_surfaces: "Public consent & cookie banners",
  consent_guardian: "Guardian consent (under-18)",
  grievance: "Grievance redressal",
  retention: "Retention & purge",
  reports: "Reports",
  audit: "Audit trail",
  breach: "Breach management",
  notifications: "Notifications",
  users: "Users & roles",
  connections: "Connections",
};

/**
 * Where a module is live but not *entirely* live. Surfaced on the screen and on
 * the roadmap. A caveat is not a footnote — an unstated exception is the same
 * mis-sell the preview banner exists to prevent.
 */
export const MODULE_CAVEATS = {
  retention:
    "Purging masks a person's identifiers and keeps their consent records, " +
    "matching the DSAR erasure path. Policies can be created, edited and run. " +
    "It reaches this product's own tables, not " +
    "a customer's connected systems. Pre-purge warnings are now sent daily by the " +
    "scheduler for policies set to auto-delete. The purge itself is never " +
    "automatic: unattended data destruction on a timer is a different risk from " +
    "sending a warning, so the notice period exists for a human to act on and the " +
    "destruction stays a human action.",
  grievance:
    "Filing, triage, the statutory clock and automatic escalation are real, and " +
    "anyone can file without an account. Escalation now runs on a schedule as " +
    "well as when the queue is read, so a complaint nobody looks at still reaches " +
    "the Grievance Officer. Two limits remain: an anonymous complaint will not " +
    "escalate until the filer confirms their email, and if they never do it needs " +
    "picking up by hand; and there is no attachment support, so a person cannot " +
    "submit supporting documents.",
  breach:
    "The register, both halves of the §8(6) notification duty, and a resumable " +
    "bulk send are real. Two limits by design rather than by omission: the " +
    "product does not submit to the Data Protection Board — it generates the " +
    "text and records that a named person submitted it, because unattended " +
    "software contacting a regulator is not something it should do — and the " +
    "72-hour countdown is this product's reading of \u201cwithout delay\u201d, not a " +
    "figure from the statute. There is no breach detection; this is a register, " +
    "not a monitoring system.",
  users:
    "Invitations, role changes, session management and the capability matrix are " +
    "real. Nobody can set anybody else's password — people are invited and choose " +
    "their own, which is what keeps an audit entry attributable to one person. " +
    "Two limits: the invite link is displayed in the console as well as emailed, " +
    "because the default notification provider writes to a log rather than " +
    "sending; and there is no SSO and no MFA enrolment flow, so `require_mfa` on " +
    "the workspace has nothing to enrol against yet.",
  reports:
    "Six registers, every figure from a query, streamed as CSV or JSON and never " +
    "stored. Three limits: no PDF yet, so a DPO forwarding one to a board sends a " +
    "spreadsheet; nothing is digitally signed — each report carries the audit " +
    "chain head hash, which is tamper evidence you can recompute, and the report " +
    "says so; and periods are capped at 366 days with exports capped at 50,000 " +
    "rows, stated in the provenance block whenever a cap is hit.",
  notifications:
    "Templates, rendering, the queue and the delivery log are real, and messages " +
    "go out on request received / completed / refused, consent withdrawal and " +
    "pre-purge warnings, plus breach notices and user invitations. Retries are " +
    "automatic now — the scheduler drains the queue every minute — so a message " +
    "that hits a transient failure no longer waits for somebody to press " +
    "\u201cProcess queue now\u201d. Two limits remain: the provider shipped by " +
    "default writes to the server log instead of sending, which the screen says at " +
    "the top; and SMS is modelled but no SMS provider is implemented.",
  consent:
    "Purposes, notices, collection, withdrawal and validation all run against the "
    + "real record, and every validation check from the console is written to the "
    + "audit chain. One thing is not real: the consent tiles and the consent chart "
    + "on the admin dashboard are still sample data, marked as such — they need an "
    + "aggregate endpoint that does not exist yet.",
  audit:
    "The chain detects any entry being edited, removed or reordered. It cannot " +
    "yet detect removal of the most recent entries — that needs external " +
    "anchoring, and the screen says so next to the result.",
  dsar_workflow:
    "The triage queue reads the real record and the person is emailed when a "
    + "request is received, completed or refused. Intermediate transitions are "
    + "deliberately silent: a message for every internal step trains people to "
    + "ignore the one that matters. The identity check offered on the request form "
    + "is still simulated, not a real verification — the queue reports whether "
    + "verification actually happened rather than assuming it did.",
  dsar:
    "Access and erasure run for real against four datastores. Two parts are not: " +
    "correction is sample data (the engine has no correction action yet), and " +
    "the OTP / DigiLocker identity check is simulated, not a real verification.",
  // "live" describes the FEATURE, not the catalogue, and the distinction is the
  // whole point of this caveat. Storing credentials, encrypting them, testing a
  // connection and auditing all of it is real. Connecting to forty named systems
  // is not — three do, and the screen says so on every card rather than in a
  // footnote.
  connections:
    "Credentials are encrypted with AES-256-GCM before storage, never returned " +
    "by any endpoint, and shown back only as a last-4 hint. A connection reads " +
    "'Connected' only after a real probe reached the system \u2014 storing a " +
    "password never sets it. Three of the forty listed connectors are " +
    "implemented and verifiable (PostgreSQL, MySQL, MongoDB); the rest are " +
    "listed with the reason each is not ready, and the server refuses " +
    "credentials for them rather than holding a secret it cannot use. " +
    "Connecting a database on a private network also needs network access, " +
    "which a password does not provide.",
};

/** What each preview module needs before it can be called live. */
export const MODULE_ROADMAP = {
  consent_guardian: "Verifiable parental consent — a real guardian identity check (DigiLocker or equivalent), which a publishable key cannot perform.",
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
  ["/admin/connections", "connections"],
  ["/admin/dsar", "dsar_workflow"],
  ["/user/consent-history", "consent"],
  ["/user/preferences", "consent"],
  ["/user/grievance", "grievance"],
  ["/user/dsar", "dsar"],
  ["/consent-banner", "consent_surfaces"],
  ["/cookie-consent", "consent_surfaces"],
];

export function moduleForPath(pathname) {
  const hit = PATH_MODULES.find(([prefix]) => (pathname || "").startsWith(prefix));
  return hit ? hit[1] : null;
}
