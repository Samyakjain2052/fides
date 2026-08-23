// ============================================================================
// Shared vocabulary, and the last few stubs that are still stubs.
//
// This file used to be the mock backend: ~500 lines holding an in-memory user,
// organisation, consent ledger, DSAR queue and audit log, plus the functions
// that read and mutated them. Every one of those has been replaced by a real
// endpoint and now lives in a sibling module — auth.js, consent.js, dsar.js,
// grievances.js, breaches.js, retention.js, reports.js, audit.js,
// notifications.js, users.js, banner.js.
//
// What was deleted, and where the last of it was still rendering:
//
//   MOCK_ORG           the user-facing header printed "Example Fintech Pvt.
//                      Ltd." and the footer named "Amit Kumar · dpo@example.com"
//                      as the Grievance Officer — to every person, in every
//                      workspace. §13 requires that contact be real and
//                      published; an invented one is a right nobody can
//                      exercise. Now read from the session and
//                      /v1/grievances/officer, and it says so plainly when no
//                      officer has been published.
//   MOCK_USER          the greeting called everybody Priya.
//   MOCK_NOTICES       labelled the activity feed on the user dashboard.
//   MOCK_CONSENTS,
//   MOCK_AUDIT_LOGS,
//   getUserDashboard,  the user dashboard's four cards and its timeline were
//   getAdminDashboard  computed from those arrays.
//   appendAuditLog,
//   verifyLogIntegrity,
//   fakeHash           a mock audit chain with invented hashes. The real one is
//                      HMAC-chained server-side and verifiable at
//                      /v1/audit/verify.
//   GRIEVANCE_CATEGORIES  a second, different list from the server's five.
//
// WHAT IS LEFT, AND WHY EACH ONE IS STILL HERE
//
// The identity-verification calls below are placeholders, and they are the
// honest kind: `consent_guardian` is declared `preview` in config/modules.js and
// the DSAR module's caveat states the identity check is simulated. They are kept
// rather than deleted because deleting them would take the screens with them,
// and a screen that says "this step is not implemented" is more useful than no
// screen. They must not be described as anything else.
//
// `sendResetLink` is in the same category: there is no password-reset flow on the
// server yet, so it resolves without sending anything.
// ============================================================================

const delay = (ms = 220) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------------------
// Vocabulary shared across screens.
// ---------------------------------------------------------------------------

// The Eighth Schedule languages, English first.
export const LANGUAGES = [
  "English", "Hindi", "Bengali", "Telugu", "Marathi", "Tamil", "Urdu",
  "Gujarati", "Kannada", "Odia", "Malayalam", "Punjabi", "Assamese",
  "Maithili", "Santali", "Kashmiri", "Nepali", "Sindhi", "Dogri",
  "Konkani", "Manipuri", "Bodo", "Sanskrit",
];

export const ROLES = [
  { id: "data_principal", label: "Data Principal", home: "/user/dashboard" },
  { id: "admin", label: "Admin / DPO", home: "/admin/dashboard" },
  { id: "auditor", label: "Auditor", home: "/admin/audit" },
  { id: "grievance_officer", label: "Grievance Officer", home: "/admin/grievances" },
];

export const COOKIE_CATEGORIES = [
  { id: "essential", name: "Essential Cookies", locked: true, description: "Required for the site to work — login, security, and session handling. These cannot be switched off." },
  { id: "performance", name: "Performance Cookies", locked: false, description: "Help us measure page speed and errors so we can fix problems." },
  { id: "analytics", name: "Analytics Cookies", locked: false, description: "Anonymised usage statistics that tell us which features people use." },
  { id: "marketing", name: "Marketing Cookies", locked: false, description: "Used to show you relevant offers on other websites." },
];

export const ERASURE_REASONS = [
  "I no longer use this service",
  "I never consented to this processing",
  "The purpose it was collected for has ended",
  "I object to how my data is being used",
  "Other",
];

export const CORRECTABLE_FIELDS = [
  "Full name", "Email address", "Phone", "Postal address", "Date of birth",
];

// ---------------------------------------------------------------------------
// Stubs. Each one is reachable only from a screen that declares it.
// ---------------------------------------------------------------------------

/**
 * No password reset exists on the server, so this sends nothing.
 *
 * It validates the field and resolves, which is also what a real implementation
 * would look like from here — a reset endpoint must not reveal whether an
 * address is registered. The difference is that nothing arrives.
 */
export async function sendResetLink(email) {
  await delay(500);
  if (!email) throw new Error("Enter your registered email address.");
  return { sent: true, email };
}

/** Simulated. Accepts any six digits — see the DSAR module's caveat. */
export async function sendOtp(destination) {
  await delay(500);
  return {
    sent: true,
    destination,
    hint: "Any 6 digits are accepted — this step is simulated.",
  };
}

export async function verifyOtp(code) {
  await delay(500);
  if (!/^\d{6}$/.test(code || "")) throw new Error("Enter the 6-digit code.");
  return { verified: true, method: "otp" };
}

/** No DigiLocker integration exists. Returns success without calling anything. */
export async function verifyDigiLocker() {
  await delay(800);
  return {
    verified: true,
    method: "digilocker",
    note: "Placeholder response — no real DigiLocker call was made.",
  };
}

/**
 * Guardian consent, §9 — the `consent_guardian` module, declared `preview`.
 *
 * Records nothing. Verifying that an adult is who they say they are, and that
 * they are this child's guardian, is the entire problem, and a publishable key
 * in a browser cannot do it. Shipping this as though it worked would be the most
 * consequential false claim in the product.
 */
export async function submitGuardianConsent({ guardianEmail, childName }) {
  await delay(600);
  if (!guardianEmail) throw new Error("Enter the guardian's email address.");
  return { requested: true, guardianEmail, childName, recorded: false };
}
