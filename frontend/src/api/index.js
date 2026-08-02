// ============================================================================
// All mock data and API functions.
// Replace each function with real fetch() calls when the backend is ready.
//
// Everything below is in-memory and mutable, so a change made on one screen is
// visible on the next one for the rest of the session (a page reload resets it).
// Every mutation also appends an audit-log entry, because the brief requires the
// audit trail to reflect every state change.
// ============================================================================

// ---------------------------------------------------------------------------
// OPTIONAL BRIDGE TO THE REAL DSAR BACKEND IN THIS REPO
//
// Default is `false` — mock data, exactly as the brief specifies. Set it to
// `true` and the three DSAR functions below talk to the FastAPI gateway
// (Fides + Postgres + Mongo) that lives in this repo instead of returning mock
// rows. Vite proxies /gateway to it, so there is no CORS to configure.
// See ../../vite.config.js and the repo README.
// ---------------------------------------------------------------------------
export const USE_REAL_DSAR_BACKEND = true;
const GATEWAY = "/gateway";

const delay = (ms = 220) => new Promise((r) => setTimeout(r, ms));
const clone = (v) => JSON.parse(JSON.stringify(v));
const nowIso = () => new Date().toISOString();

// ============================================================================
// MOCK DATA
// ============================================================================

export const MOCK_USER = {
  id: "u001",
  name: "Priya Sharma",
  email: "priya@example.com",
  language: "en",
};

// The Data Principal these screens act as.
//
// This used to be MOCK_USER everywhere, including on the DSAR path that now
// really executes — so a buyer who signed up as themselves would have fired an
// access request against priya@example.com and been shown her requests as their
// own. Wrong data to the wrong person, from a product selling privacy.
//
// AppContext sets this from the real session. The fallback keeps the preview
// modules working for a signed-out visitor on the standalone consent surfaces.
let subject = { ...MOCK_USER };

export function setSubjectIdentity(user) {
  subject = user
    ? { id: user.id, name: user.name, email: user.email, language: user.language || "en" }
    : { ...MOCK_USER };
}

export function subjectIdentity() {
  return { ...subject };
}

export const MOCK_ORG = {
  id: "org001",
  name: "Example Fintech Pvt. Ltd.",
  grievanceOfficer: "Amit Kumar",
  grievanceEmail: "dpo@example.com",
};

// The Eighth Schedule languages, English first.
export const LANGUAGES = [
  "English", "Hindi", "Bengali", "Telugu", "Marathi", "Tamil", "Urdu",
  "Gujarati", "Kannada", "Odia", "Malayalam", "Punjabi", "Assamese",
  "Maithili", "Santali", "Kashmiri", "Nepali", "Sindhi", "Dogri",
  "Konkani", "Manipuri", "Bodo", "Sanskrit",
];

export const MOCK_NOTICES = [
  {
    id: "n1", purpose: "Account Creation", category: "Identity Data",
    retention_days: 1825, mandatory: true,
    content: "We collect your name, email, and phone to create your account.",
    data_collected: "Name, Email, Phone",
    user_rights: "You may access, correct or erase this data anytime.",
    withdrawal_policy: "Account will be deactivated upon withdrawal.",
  },
  {
    id: "n2", purpose: "Marketing Communications", category: "Contact Data",
    retention_days: 730, mandatory: false,
    content: "We use your email to send product updates and offers.",
    data_collected: "Email address",
    user_rights: "You may withdraw consent anytime.",
    withdrawal_policy: "You will stop receiving marketing emails within 24 hours.",
  },
  {
    id: "n3", purpose: "Analytics", category: "Usage Data",
    retention_days: 365, mandatory: false,
    content: "We use anonymized usage data to improve our product.",
    data_collected: "Usage patterns, device info",
    user_rights: "You may withdraw at any time.",
    withdrawal_policy: "Analytics tracking will stop immediately.",
  },
  {
    id: "n4", purpose: "KYC Verification", category: "Sensitive Identity Data",
    retention_days: 2555, mandatory: true,
    content: "As required by RBI, we collect Aadhaar and PAN for identity verification.",
    data_collected: "Aadhaar number, PAN card",
    user_rights: "Required by law. Limited withdrawal rights.",
    withdrawal_policy: "May affect your ability to use financial services.",
  },
];

let consents = [
  { id: "c1", user_id: "u001", notice_id: "n1", purpose: "Account Creation", status: "active", given_at: "2026-01-15T10:00:00Z", expires_at: "2031-01-15T10:00:00Z", language: "en", version: "1.0", method: "checkbox" },
  { id: "c2", user_id: "u001", notice_id: "n2", purpose: "Marketing Communications", status: "withdrawn", given_at: "2026-01-15T10:01:00Z", withdrawn_at: "2026-05-01T09:00:00Z", language: "en", version: "1.0", method: "checkbox" },
  { id: "c3", user_id: "u001", notice_id: "n3", purpose: "Analytics", status: "active", given_at: "2026-01-15T10:02:00Z", expires_at: "2027-01-15T10:02:00Z", language: "en", version: "1.0", method: "checkbox" },
  { id: "c4", user_id: "u001", notice_id: "n4", purpose: "KYC Verification", status: "active", given_at: "2026-01-15T10:03:00Z", expires_at: "2033-01-15T10:03:00Z", language: "en", version: "1.0", method: "checkbox" },
];
export const MOCK_CONSENTS = clone(consents);

// Starts EMPTY, unlike every other fixture here.
//
// DSAR is the one module marked "live", and requests submitted from these
// screens really execute against the Fides engine. Seeding it with three
// invented requests would have put fabricated rows in the same list as real
// ones, and made the dashboard's "open requests" count — which is presented as
// a real figure — a mix of the two. An empty queue is the honest starting state.
let dsarRequests = [];
export const MOCK_DSAR_REQUESTS = clone(dsarRequests);

let grievances = [
  { id: "g1", user_id: "u001", user_email: "priya@example.com", category: "Consent Violation", description: "I withdrew marketing consent but still received emails.", status: "in_progress", submitted_at: "2026-07-01T08:00:00Z", reference: "GRV-2026-001", related_dsar: null, officer: "Meena Patel", resolution_notes: "", escalated: false, feedback: null },
  { id: "g2", user_id: "u002", user_email: "rahul@example.com", category: "Data Breach", description: "I received a notification about my data being accessed without consent.", status: "open", submitted_at: "2026-07-15T08:00:00Z", reference: "GRV-2026-002", related_dsar: "d1", officer: "Meena Patel", resolution_notes: "", escalated: false, feedback: null },
];
export const MOCK_GRIEVANCES = clone(grievances);

let auditLogs = [
  { id: "a1", log_id: "LOG-001", user_id: "u001", purpose_id: "n2", action_type: "withdraw", timestamp: "2026-05-01T09:00:00Z", consent_status: "withdrawn", initiator: "user", source_ip: "192.168.1.1", audit_hash: "sha256:abc123def456..." },
  { id: "a2", log_id: "LOG-002", user_id: "u001", purpose_id: "n1", action_type: "grant", timestamp: "2026-01-15T10:00:00Z", consent_status: "active", initiator: "user", source_ip: "192.168.1.1", audit_hash: "sha256:xyz789ghi012..." },
  { id: "a3", log_id: "LOG-003", user_id: "u002", purpose_id: "n4", action_type: "validate", timestamp: "2026-07-20T14:00:00Z", consent_status: "active", initiator: "system", source_ip: "10.0.0.1", audit_hash: "sha256:mno345pqr678..." },
];
export const MOCK_AUDIT_LOGS = clone(auditLogs);

let usersAdmin = [
  { id: "u001", name: "Priya Sharma", email: "priya@example.com", role: "data_principal", created_at: "2026-01-15", mfa: true, active: true },
  { id: "adm01", name: "Amit Kumar", email: "amit@example.com", role: "admin", created_at: "2025-12-01", mfa: true, active: true },
  { id: "aud01", name: "Ravi Joshi", email: "ravi@example.com", role: "auditor", created_at: "2025-12-01", mfa: false, active: true },
  { id: "grv01", name: "Meena Patel", email: "meena@example.com", role: "grievance_officer", created_at: "2025-12-01", mfa: true, active: true },
];
export const MOCK_USERS_ADMIN = clone(usersAdmin);

let retentionPolicies = [
  { id: "rp1", category: "Identity Data", retention_days: 1825, auto_delete: true, exemption: "Retain if RBI mandates", last_purge: "2026-07-01", notify_days: 7 },
  { id: "rp2", category: "Marketing Data", retention_days: 730, auto_delete: true, exemption: null, last_purge: "2026-06-15", notify_days: 14 },
];
export const MOCK_RETENTION_POLICIES = clone(retentionPolicies);

// --- supporting mock data the screens need ----------------------------------

export const COOKIE_CATEGORIES = [
  { id: "essential", name: "Essential Cookies", locked: true, description: "Required for the site to work — login, security, and session handling. These cannot be switched off." },
  { id: "performance", name: "Performance Cookies", locked: false, description: "Help us measure page speed and errors so we can fix problems." },
  { id: "analytics", name: "Analytics Cookies", locked: false, description: "Anonymised usage statistics that tell us which features people use." },
  { id: "marketing", name: "Marketing Cookies", locked: false, description: "Used to show you relevant offers on other websites." },
];

export const GRIEVANCE_CATEGORIES = [
  "Consent Violation", "Data Breach", "Processing Error",
  "Unauthorized Data Sharing", "Delayed DSAR Response", "Other",
];

export const ERASURE_REASONS = [
  "I no longer use this service",
  "I never consented to this processing",
  "The purpose it was collected for has ended",
  "I object to how my data is being used",
  "Other",
];

export const CORRECTABLE_FIELDS = ["Full name", "Email address", "Phone", "Postal address", "Date of birth"];

let notifications = [
  { id: "nt1", audience: "user", to: "priya@example.com", subject: "Consent withdrawal confirmed", scenario: "withdrawal_confirmation", channel: "Email", status: "delivered", sent_at: "2026-05-01T09:01:00Z", language: "English" },
  { id: "nt2", audience: "user", to: "priya@example.com", subject: "Your data request DSAR-2026-001 is complete", scenario: "dsar_update", channel: "Email", status: "delivered", sent_at: "2026-06-20T08:05:00Z", language: "English" },
  { id: "nt3", audience: "user", to: "rahul@example.com", subject: "Consent renewal due in 30 days", scenario: "renewal_reminder", channel: "SMS", status: "failed", sent_at: "2026-07-18T06:00:00Z", language: "Hindi" },
  { id: "nt4", audience: "user", to: "anita@example.com", subject: "We received your correction request", scenario: "dsar_update", channel: "In-App", status: "pending", sent_at: "2026-07-20T08:01:00Z", language: "English" },
  { id: "nt5", audience: "fiduciary", to: "billing-processor.example.com", subject: "Consent withdrawn: Marketing Communications (u001)", scenario: "withdrawal_alert", channel: "Webhook", status: "delivered", http_status: 200, sent_at: "2026-05-01T09:00:30Z", acknowledged: true },
  { id: "nt6", audience: "fiduciary", to: "analytics-processor.example.com", subject: "Validation request: u002 / n4", scenario: "validation_request", channel: "Webhook", status: "failed", http_status: 500, sent_at: "2026-07-20T14:00:05Z", acknowledged: false, escalated: true },
];

export const NOTIFICATION_TEMPLATES = [
  { id: "t1", scenario: "consent_confirmation", subject: "Your consent choices have been saved", body: "Hello {{name}}, we have recorded your consent for {{purpose}} on {{date}}. You can change this any time in your Preference Centre." },
  { id: "t2", scenario: "withdrawal_confirmation", subject: "Consent withdrawal confirmed", body: "Hello {{name}}, your consent for {{purpose}} was withdrawn on {{date}}. {{withdrawal_policy}}" },
  { id: "t3", scenario: "dsar_update", subject: "Update on your data request {{reference}}", body: "Hello {{name}}, your {{type}} request is now {{status}}. We will respond by {{deadline}}." },
  { id: "t4", scenario: "renewal_reminder", subject: "Consent renewal due", body: "Hello {{name}}, your consent for {{purpose}} expires on {{expires_at}}. No action means it will lapse." },
];

let breaches = [
  { id: "b1", reference: "BRE-2026-001", detected_at: "2026-07-22T02:15:00Z", reported_at: "2026-07-22T09:40:00Z", severity: "high", status: "reported_to_dpb", affected_users: 1420, categories: "Contact Data", description: "Misconfigured storage bucket exposed marketing contact exports for ~6 hours.", remediation: "Bucket policy corrected, access logs reviewed, affected users notified." },
  { id: "b2", reference: "BRE-2026-002", detected_at: "2026-07-28T18:05:00Z", reported_at: null, severity: "medium", status: "investigating", affected_users: 12, categories: "Usage Data", description: "Support agent accessed session records outside assigned ticket scope.", remediation: "Access revoked pending review." },
];

let validationLog = [
  { id: "v1", user_id: "u001", purpose_id: "n1", result: "valid", checked_at: "2026-07-29T09:12:00Z", caller: "core-banking-api" },
  { id: "v2", user_id: "u001", purpose_id: "n2", result: "withdrawn", checked_at: "2026-07-29T09:12:04Z", caller: "marketing-service" },
  { id: "v3", user_id: "u003", purpose_id: "n3", result: "expired", checked_at: "2026-07-29T10:01:00Z", caller: "analytics-service" },
];

let reports = [
  { id: "r1", type: "Consent Report", generated_at: "2026-07-01T05:00:00Z", generated_by: "adm01", range: "Jun 2026", format: "PDF", signed: false },
  { id: "r2", type: "Audit Report", generated_at: "2026-07-01T05:02:00Z", generated_by: "adm01", range: "Jun 2026", format: "PDF", signed: true },
];

// Chart series for the admin dashboard.
// Removed: a hardcoded [24, 9, 14] rendered under the heading "Last 30 days".
// It was not derived from anything — it was three numbers that looked like a
// measurement. On a compliance dashboard that is the most damaging artifact in
// the product, so the chart now derives from real requests and renders an
// honest empty state when there are none.

// Removed with the same reasoning: a six-month "given vs withdrawn" trend
// climbing 320 -> 548 is a claim about a history that never happened, and a
// trend line is a stronger claim than a single number because it implies
// sustained real usage.

// ============================================================================
// AUDIT TRAIL — every mutation lands here
// ============================================================================
let logSeq = auditLogs.length;

function fakeHash(seed) {
  // Presentation-only stand-in for a real signed hash chain.
  let h = 0;
  const s = String(seed) + nowIso();
  for (let i = 0; i < s.length; i += 1) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return "sha256:" + h.toString(16).padStart(8, "0").repeat(4).slice(0, 40) + "...";
}

export function appendAuditLog(entry) {
  logSeq += 1;
  const row = {
    id: "a" + logSeq,
    log_id: "LOG-" + String(logSeq).padStart(3, "0"),
    timestamp: nowIso(),
    initiator: "user",
    source_ip: "192.168.1.1",
    consent_status: "-",
    purpose_id: "-",
    user_id: subject.id,
    ...entry,
    audit_hash: fakeHash(entry.action_type),
  };
  auditLogs = [row, ...auditLogs];
  return row;
}

export async function getAuditLogs(filters = {}) {
  await delay();
  let rows = clone(auditLogs);
  const { action_type, user_id, purpose_id, initiator, from, to } = filters;
  if (action_type) rows = rows.filter((r) => r.action_type === action_type);
  if (user_id) rows = rows.filter((r) => r.user_id.toLowerCase().includes(user_id.toLowerCase()));
  if (purpose_id) rows = rows.filter((r) => r.purpose_id === purpose_id);
  if (initiator) rows = rows.filter((r) => r.initiator === initiator);
  if (from) rows = rows.filter((r) => r.timestamp >= from);
  if (to) rows = rows.filter((r) => r.timestamp <= to);
  return rows;
}

export async function verifyLogIntegrity() {
  await delay(600);
  return { ok: true, checked: auditLogs.length, broken: [], verified_at: nowIso() };
}

// ============================================================================
// AUTH
// ============================================================================
export const ROLES = [
  { id: "data_principal", label: "Data Principal", home: "/user/dashboard" },
  { id: "admin", label: "Admin / DPO", home: "/admin/dashboard" },
  { id: "auditor", label: "Auditor", home: "/admin/audit" },
  { id: "grievance_officer", label: "Grievance Officer", home: "/admin/grievances" },
];

export async function login({ email, password, role }) {
  await delay(400);
  if (!email || !password) throw new Error("Email and password are required.");
  const known = usersAdmin.find((u) => u.email === email);
  const profile = {
    id: known?.id || (role === "data_principal" ? subject.id : "adm01"),
    name: known?.name || (role === "data_principal" ? subject.name : "Compliance Officer"),
    email,
    role,
  };
  appendAuditLog({ action_type: "login", user_id: profile.id, initiator: "user" });
  return profile;
}

export async function sendResetLink(email) {
  await delay(500);
  if (!email) throw new Error("Enter your registered email address.");
  return { sent: true, email };
}

// Identity verification — UI only, per the brief.
export async function sendOtp(destination) {
  await delay(500);
  return { sent: true, destination, hint: "Any 6 digits are accepted in this mock." };
}

export async function verifyOtp(code) {
  await delay(500);
  if (!/^\d{6}$/.test(code || "")) throw new Error("Enter the 6-digit code.");
  return { verified: true, method: "otp" };
}

export async function verifyDigiLocker() {
  await delay(800);
  return { verified: true, method: "digilocker", note: "Placeholder response — no real DigiLocker call." };
}

// ============================================================================
// NOTICES & CONSENT
// ============================================================================
export async function getNotices() {
  await delay();
  return clone(MOCK_NOTICES);
}

export async function getConsents(userId = subject.id) {
  await delay();
  return clone(consents.filter((c) => c.user_id === userId));
}

export async function updateConsent(consentId, nextStatus) {
  await delay(300);
  const row = consents.find((c) => c.id === consentId);
  if (!row) throw new Error("Consent not found.");
  row.status = nextStatus;
  if (nextStatus === "withdrawn") row.withdrawn_at = nowIso();
  if (nextStatus === "active") {
    row.given_at = nowIso();
    delete row.withdrawn_at;
  }
  const log = appendAuditLog({
    action_type: nextStatus === "withdrawn" ? "withdraw" : "grant",
    purpose_id: row.notice_id,
    consent_status: row.status,
    user_id: row.user_id,
  });
  return { consent: clone(row), audit: log };
}

// Saving the consent banner: one consent row per purpose.
export async function saveConsentChoices(choices, { language = "English", method = "checkbox" } = {}) {
  await delay(400);
  const audits = [];
  Object.entries(choices).forEach(([noticeId, granted]) => {
    const notice = MOCK_NOTICES.find((n) => n.id === noticeId);
    if (!notice) return;
    let row = consents.find((c) => c.notice_id === noticeId && c.user_id === subject.id);
    const status = granted ? "active" : "withdrawn";
    if (!row) {
      row = {
        id: "c" + (consents.length + 1),
        user_id: subject.id,
        notice_id: noticeId,
        purpose: notice.purpose,
        status,
        given_at: nowIso(),
        expires_at: new Date(Date.now() + notice.retention_days * 864e5).toISOString(),
        language,
        version: "1.0",
        method,
      };
      consents.push(row);
    } else {
      row.status = status;
      row.language = language;
      row.method = method;
      if (granted) row.given_at = nowIso();
      else row.withdrawn_at = nowIso();
    }
    audits.push(
      appendAuditLog({
        action_type: granted ? "grant" : "withdraw",
        purpose_id: noticeId,
        consent_status: status,
      })
    );
  });
  return { saved: true, audits };
}

export async function saveCookiePreferences(prefs) {
  await delay(300);
  const audit = appendAuditLog({ action_type: "cookie_preferences", purpose_id: "cookies", consent_status: "recorded" });
  return { saved: true, prefs, audit, renews_at: new Date(Date.now() + 365 * 864e5).toISOString() };
}

export async function submitGuardianConsent({ guardianEmail, childName }) {
  await delay(600);
  const audit = appendAuditLog({ action_type: "guardian_consent_requested", purpose_id: "-", consent_status: "pending" });
  return { sent: true, guardianEmail, childName, audit };
}

// Consent history: derived from the audit trail, which is the real source.
export async function getConsentHistory(userId = subject.id) {
  await delay();
  const consentActions = ["grant", "withdraw", "update", "renew"];
  return clone(auditLogs)
    .filter((l) => l.user_id === userId && consentActions.includes(l.action_type))
    .map((l) => {
      const notice = MOCK_NOTICES.find((n) => n.id === l.purpose_id);
      const consent = consents.find((c) => c.notice_id === l.purpose_id);
      return {
        ...l,
        purpose: notice?.purpose || l.purpose_id,
        method: consent?.method || "checkbox",
        version: consent?.version || "1.0",
      };
    });
}

export async function validateConsent({ userId, purposeId }) {
  await delay(350);
  const row = consents.find((c) => c.user_id === userId && c.notice_id === purposeId);
  let result = "invalid";
  if (row) {
    if (row.status === "withdrawn") result = "withdrawn";
    else if (row.expires_at && row.expires_at < nowIso()) result = "expired";
    else if (row.status === "active") result = "valid";
  }
  const entry = { id: "v" + (validationLog.length + 1), user_id: userId, purpose_id: purposeId, result, checked_at: nowIso(), caller: "admin-console" };
  validationLog = [entry, ...validationLog];
  appendAuditLog({ action_type: "validate", user_id: userId, purpose_id: purposeId, consent_status: row?.status || "none", initiator: "Data Fiduciary" });
  return {
    result,
    purpose_alignment: row ? "matches declared purpose" : "no consent record",
    timestamp_valid: Boolean(row),
    consent_status: row?.status || "none",
  };
}

export async function getValidationLog() {
  await delay();
  return clone(validationLog);
}

export async function bulkValidate(pairs) {
  await delay(700);
  const out = [];
  for (const p of pairs) out.push({ ...p, ...(await validateConsent(p)) });
  return out;
}

// ============================================================================
// DSAR
// ============================================================================
const DSAR_SLA_DAYS = 30;

export function deadlineFor(submittedAt) {
  return new Date(new Date(submittedAt).getTime() + DSAR_SLA_DAYS * 864e5).toISOString();
}

export async function submitDSAR({ type, verification, details = {} }) {
  // --- real backend path (opt-in) -----------------------------------------
  if (USE_REAL_DSAR_BACKEND && (type === "access" || type === "erase")) {
    const resp = await fetch(`${GATEWAY}/dsar`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: subject.email, action: type === "erase" ? "erasure" : "access" }),
    });
    if (!resp.ok) throw new Error("Gateway rejected the request");
    const created = await resp.json();
    const row = {
      id: created.request_id, user_id: subject.id, user_email: subject.email,
      type, status: "in_progress", submitted_at: nowIso(),
      deadline_at: deadlineFor(nowIso()), reference: created.request_id,
      verification, details, real: true,
    };
    dsarRequests = [row, ...dsarRequests];
    appendAuditLog({ action_type: "dsar_submitted", purpose_id: "-", consent_status: type });
    return clone(row);
  }

  // --- mock path (default) ------------------------------------------------
  await delay(500);
  const n = dsarRequests.length + 1;
  const submitted = nowIso();
  const row = {
    id: "d" + n,
    user_id: subject.id,
    user_email: subject.email,
    type,
    status: "pending",
    submitted_at: submitted,
    deadline_at: deadlineFor(submitted),
    reference: "DSAR-2026-" + String(n).padStart(3, "0"),
    verification,
    ...details,
  };
  dsarRequests = [row, ...dsarRequests];
  appendAuditLog({ action_type: "dsar_submitted", purpose_id: "-", consent_status: type });
  notifications = [
    { id: "nt" + (notifications.length + 1), audience: "user", to: subject.email, subject: `We received your ${type} request ${row.reference}`, scenario: "dsar_update", channel: "Email", status: "delivered", sent_at: submitted, language: "English" },
    ...notifications,
  ];
  return clone(row);
}

export async function getDSARRequests({ userId } = {}) {
  await delay();
  let rows = clone(dsarRequests);
  if (userId) rows = rows.filter((r) => r.user_id === userId);

  // Real requests get their live status from the gateway.
  if (USE_REAL_DSAR_BACKEND) {
    await Promise.all(
      rows.filter((r) => r.real).map(async (r) => {
        try {
          const resp = await fetch(`${GATEWAY}/dsar/${r.id}`);
          if (!resp.ok) return;
          const live = await resp.json();
          r.status = live.status === "complete" ? "completed" : live.status === "error" ? "rejected" : "in_progress";
          r.execution_log = live.execution_log;
          r.data = live.data;
          if (live.finished_processing_at) r.resolved_at = live.finished_processing_at;
        } catch (_) { /* keep the mock status */ }
      })
    );
  }
  return rows;
}

export async function updateDSAR(id, patch) {
  await delay(350);
  const row = dsarRequests.find((r) => r.id === id);
  if (!row) throw new Error("Request not found.");
  Object.assign(row, patch);
  if (patch.status === "completed") row.resolved_at = nowIso();
  const audit = appendAuditLog({
    action_type: "dsar_" + (patch.status || "updated"),
    user_id: row.user_id,
    purpose_id: "-",
    consent_status: row.type,
    initiator: "Data Fiduciary",
  });
  if (patch.notify) {
    notifications = [
      { id: "nt" + (notifications.length + 1), audience: "user", to: row.user_email, subject: `Update on your request ${row.reference}: ${row.status}`, scenario: "dsar_update", channel: "Email", status: "delivered", sent_at: nowIso(), language: "English" },
      ...notifications,
    ];
  }
  return { request: clone(row), audit };
}

export async function prepareDataExport(id) {
  await delay(900);
  const row = dsarRequests.find((r) => r.id === id);
  if (row) row.export_url = "#mock-export-" + row.reference;
  appendAuditLog({ action_type: "dsar_export_prepared", user_id: row?.user_id, initiator: "Data Fiduciary" });
  return { ready: true, url: row?.export_url };
}

// ============================================================================
// GRIEVANCES
// ============================================================================
export async function submitGrievance({ category, description, relatedDsar, language }) {
  await delay(500);
  if (!description || description.length < 50) {
    throw new Error("Please describe the issue in at least 50 characters.");
  }
  const n = grievances.length + 1;
  const row = {
    id: "g" + n,
    user_id: subject.id,
    user_email: subject.email,
    category,
    description,
    status: "open",
    submitted_at: nowIso(),
    reference: "GRV-2026-" + String(n).padStart(3, "0"),
    related_dsar: relatedDsar || null,
    officer: "Meena Patel",
    resolution_notes: "",
    escalated: false,
    feedback: null,
    language,
  };
  grievances = [row, ...grievances];
  appendAuditLog({ action_type: "grievance_submitted", purpose_id: "-", consent_status: category });
  return clone(row);
}

export async function getGrievances({ userId } = {}) {
  await delay();
  let rows = clone(grievances);
  if (userId) rows = rows.filter((r) => r.user_id === userId);
  return rows;
}

export async function updateGrievance(id, patch) {
  await delay(350);
  const row = grievances.find((r) => r.id === id);
  if (!row) throw new Error("Grievance not found.");
  Object.assign(row, patch);
  const audit = appendAuditLog({
    action_type: patch.escalated ? "grievance_escalated" : "grievance_" + (patch.status || "updated"),
    user_id: row.user_id,
    initiator: "Data Fiduciary",
    consent_status: row.status,
  });
  if (patch.notify) {
    notifications = [
      { id: "nt" + (notifications.length + 1), audience: "user", to: row.user_email, subject: `Update on your complaint ${row.reference}: ${row.status}`, scenario: "grievance_update", channel: "Email", status: "delivered", sent_at: nowIso(), language: "English" },
      ...notifications,
    ];
  }
  return { grievance: clone(row), audit };
}

export async function submitGrievanceFeedback(id, { rating, comment }) {
  await delay(300);
  const row = grievances.find((r) => r.id === id);
  if (row) row.feedback = { rating, comment, at: nowIso() };
  return clone(row);
}

// The escalation threshold the brief wants configurable.
export const GRIEVANCE_ESCALATION_DAYS = 10;

// ============================================================================
// ADMIN: users, retention, notifications, reports, breaches
// ============================================================================
export const ROLE_PERMISSIONS = [
  { capability: "View own consents & requests", data_principal: true, admin: true, auditor: false, grievance_officer: false },
  { capability: "Process DSAR queue", data_principal: false, admin: true, auditor: false, grievance_officer: false },
  { capability: "Validate consent (API)", data_principal: false, admin: true, auditor: false, grievance_officer: false },
  { capability: "Handle grievances", data_principal: false, admin: true, auditor: false, grievance_officer: true },
  { capability: "Escalate to DPO", data_principal: false, admin: true, auditor: false, grievance_officer: true },
  { capability: "View audit logs", data_principal: false, admin: true, auditor: true, grievance_officer: false },
  { capability: "Export regulator reports", data_principal: false, admin: true, auditor: true, grievance_officer: false },
  { capability: "Manage roles & MFA", data_principal: false, admin: true, auditor: false, grievance_officer: false },
  { capability: "Edit retention policy", data_principal: false, admin: true, auditor: false, grievance_officer: false },
  { capability: "Edit or delete audit logs", data_principal: false, admin: false, auditor: false, grievance_officer: false },
];

export async function getUsers() {
  await delay();
  return clone(usersAdmin);
}

export async function addUser({ name, email, role }) {
  await delay(350);
  const row = { id: "u" + (usersAdmin.length + 100), name, email, role, created_at: nowIso().slice(0, 10), mfa: false, active: true };
  usersAdmin = [...usersAdmin, row];
  appendAuditLog({ action_type: "user_created", user_id: row.id, initiator: "Data Fiduciary", consent_status: role });
  return clone(row);
}

export async function updateUser(id, patch) {
  await delay(300);
  const row = usersAdmin.find((u) => u.id === id);
  if (!row) throw new Error("User not found.");
  const before = row.role;
  Object.assign(row, patch);
  appendAuditLog({
    action_type: patch.role && patch.role !== before ? "role_changed" : patch.active === false ? "access_revoked" : "user_updated",
    user_id: id,
    initiator: "Data Fiduciary",
    consent_status: row.role,
  });
  return clone(row);
}

export async function getRetentionPolicies() {
  await delay();
  return clone(retentionPolicies);
}

export async function updateRetentionPolicy(id, patch) {
  await delay(300);
  const row = retentionPolicies.find((p) => p.id === id);
  if (!row) throw new Error("Policy not found.");
  Object.assign(row, patch);
  appendAuditLog({ action_type: "retention_policy_updated", initiator: "Data Fiduciary", consent_status: row.category });
  return clone(row);
}

export async function runPurge(id) {
  await delay(900);
  const row = retentionPolicies.find((p) => p.id === id);
  if (!row) throw new Error("Policy not found.");
  row.last_purge = nowIso().slice(0, 10);
  const deleted = Math.floor(Math.random() * 40) + 5;
  const audit = appendAuditLog({ action_type: "purge_run", initiator: "system", consent_status: row.category });
  return { category: row.category, records_deleted: deleted, at: row.last_purge, audit };
}

export async function getNotifications(audience) {
  await delay();
  return clone(audience ? notifications.filter((n) => n.audience === audience) : notifications);
}

export async function retryNotification(id) {
  await delay(600);
  const row = notifications.find((n) => n.id === id);
  if (row) {
    row.status = "delivered";
    if (row.http_status) row.http_status = 200;
    row.sent_at = nowIso();
  }
  appendAuditLog({ action_type: "notification_retried", initiator: "system" });
  return clone(row);
}

export async function sendTestAlert(target) {
  await delay(700);
  const row = { id: "nt" + (notifications.length + 1), audience: "fiduciary", to: target || "test-processor.example.com", subject: "Test alert", scenario: "test", channel: "Webhook", status: "delivered", http_status: 200, sent_at: nowIso(), acknowledged: true };
  notifications = [row, ...notifications];
  return clone(row);
}

export async function getBreaches() {
  await delay();
  return clone(breaches);
}

export async function saveBreach(patch) {
  await delay(400);
  if (patch.id) {
    const row = breaches.find((b) => b.id === patch.id);
    Object.assign(row, patch);
    appendAuditLog({ action_type: "breach_updated", initiator: "Data Fiduciary", consent_status: row.status });
    return clone(row);
  }
  const n = breaches.length + 1;
  const row = { id: "b" + n, reference: "BRE-2026-" + String(n).padStart(3, "0"), detected_at: nowIso(), reported_at: null, status: "investigating", ...patch };
  breaches = [row, ...breaches];
  appendAuditLog({ action_type: "breach_recorded", initiator: "Data Fiduciary", consent_status: row.severity });
  return clone(row);
}

export async function getReports() {
  await delay();
  return clone(reports);
}

export async function generateReport({ type, range, format, signed, by }) {
  await delay(900);
  const row = { id: "r" + (reports.length + 1), type, range, format, signed: Boolean(signed), generated_at: nowIso(), generated_by: by || "adm01" };
  reports = [row, ...reports];
  appendAuditLog({ action_type: "report_generated", initiator: "Data Fiduciary", consent_status: type });
  return clone(row);
}

// ============================================================================
// DASHBOARD AGGREGATES
// ============================================================================
const DAY = 864e5;

export async function getUserDashboard(userId = subject.id) {
  await delay();
  const mine = consents.filter((c) => c.user_id === userId);
  const soon = mine.filter(
    (c) => c.status === "active" && c.expires_at && new Date(c.expires_at) - Date.now() < 30 * DAY
  );
  const myDsar = dsarRequests.filter((r) => r.user_id === userId);
  const myGrv = grievances.filter((g) => g.user_id === userId);
  const recent = clone(auditLogs).filter((l) => l.user_id === userId).slice(0, 5);
  return {
    active_consents: mine.filter((c) => c.status === "active").length,
    pending_dsar: myDsar.filter((r) => r.status !== "completed" && r.status !== "rejected").length,
    open_grievances: myGrv.filter((g) => g.status !== "resolved").length,
    expiring_soon: soon.length,
    recent_activity: recent,
  };
}

export async function getAdminDashboard() {
  await delay();
  const monthAgo = new Date(Date.now() - 30 * DAY).toISOString();
  const overdue = dsarRequests.filter(
    (r) => r.status !== "completed" && r.status !== "rejected" && r.deadline_at < nowIso()
  );
  const dueSoon = dsarRequests.filter((r) => {
    if (r.status === "completed" || r.status === "rejected") return false;
    const left = new Date(r.deadline_at) - Date.now();
    return left > 0 && left < 5 * DAY;
  });
  const staleGrievances = grievances.filter(
    (g) => g.status !== "resolved" && Date.now() - new Date(g.submitted_at) > GRIEVANCE_ESCALATION_DAYS * DAY
  );
  const expiring7 = consents.filter(
    (c) => c.status === "active" && c.expires_at && new Date(c.expires_at) - Date.now() < 7 * DAY
  );
  const expiring30 = consents.filter(
    (c) => c.status === "active" && c.expires_at && new Date(c.expires_at) - Date.now() < 30 * DAY
  );
  return {
    stats: {
      active_consents: consents.filter((c) => c.status === "active").length,
      withdrawn_this_month: consents.filter((c) => c.withdrawn_at && c.withdrawn_at > monthAgo).length,
      open_dsar: dsarRequests.filter((r) => r.status !== "completed" && r.status !== "rejected").length,
      overdue_dsar: overdue.length,
      open_grievances: grievances.filter((g) => g.status !== "resolved").length,
      expiring_30: expiring30.length,
    },
    attention: {
      dsar_due_soon: clone([...overdue, ...dueSoon]),
      stale_grievances: clone(staleGrievances),
      consents_expiring_7: clone(expiring7),
    },
    charts: {
      // Derived from the requests that actually exist. Empty is a legitimate
      // answer and the dashboard renders it as one, rather than filling the
      // space with numbers nobody measured.
      dsar_by_type: ["access", "correct", "erase"]
        .map((type) => ({
          label: type[0].toUpperCase() + type.slice(1),
          value: dsarRequests.filter((r) => r.type === type).length,
        }))
        .filter((d) => d.value > 0),
      status_split: [
        { label: "Active", value: consents.filter((c) => c.status === "active").length, tone: "success" },
        { label: "Withdrawn", value: consents.filter((c) => c.status === "withdrawn").length, tone: "danger" },
        { label: "Expired", value: consents.filter((c) => c.status === "expired").length, tone: "warning" },
      ].filter((d) => d.value > 0),
    },
  };
}
