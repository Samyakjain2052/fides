# LOVABLE PROMPT — Consent Management System (CMS)

## Based on MeitY / NeGD BRD for DPDP Act, 2023

## Full UI — User End \+ Admin End

---

Build a complete **Consent Management System (CMS)** web application based on India's Digital Personal Data Protection (DPDP) Act, 2023\. This is a government-grade compliance platform used by Data Fiduciaries (companies) to manage consent of their users (Data Principals). Build every screen listed below for both the **User (Data Principal) side** and the **Admin / Compliance Officer side**.

---

## Design System — Apply Across Every Screen

**Color Palette:**

- Primary Navy: `#1A3C5E`  
- Accent Teal: `#0D7377`  
- Success Green: `#22C55E`  
- Warning Amber: `#F59E0B`  
- Danger Red: `#EF4444`  
- Background: `#F8FAFC`  
- Card Surface: `#FFFFFF`  
- Border: `#E2E8F0`  
- Text Primary: `#1E293B`  
- Text Muted: `#64748B`

**Style Rules:**

- Font: Inter (Google Fonts)  
- Cards: white, `rounded-xl`, `shadow-sm`  
- Buttons: Primary \= Navy filled, Secondary \= outlined, Danger \= red outlined  
- Status badges: always colored dot \+ text label (never color alone — accessibility)  
- Every destructive action (withdraw, delete, erase) needs a confirmation modal  
- All forms multilingual-ready: language switcher visible on every user-facing screen  
- WCAG-compliant design: sufficient contrast, keyboard navigable  
- Mobile responsive throughout

**Status Colors (use consistently everywhere):**

- Active / Valid → Green  
- Pending / Expiring Soon → Amber  
- Withdrawn / Expired / Rejected → Red  
- In Progress → Blue

---

## User Roles

1. **Data Principal (End User)** — the person whose data is being processed. Sees only their own data.  
2. **Admin / DPO (Data Protection Officer)** — full access to compliance dashboard, all requests, audit logs, system settings.  
3. **Auditor** — read-only access to audit logs and reports.  
4. **Grievance Officer** — sees only grievance queue, can update resolution status.

---

## Tech Stack

- React \+ Vite  
- Tailwind CSS  
- All API calls in `src/api/index.js` (mock data to start, easy to replace with real backend)  
- React Router for navigation  
- useState/useContext for state (no Redux needed)

---

## File Structure

src/

  api/

    index.js              ← all mock API functions here

  components/

    Layout/

      UserLayout.jsx      ← sidebar \+ header for user side

      AdminLayout.jsx     ← sidebar \+ header for admin side

    common/

      StatusBadge.jsx     ← colored dot \+ text, variants: active/withdrawn/expired/pending

      ConfirmModal.jsx    ← reusable confirmation dialog for destructive actions

      LanguageSwitcher.jsx ← dropdown: English \+ 22 Indian languages (Eighth Schedule)

      SLACountdown.jsx    ← shows days remaining, color-coded green/amber/red

      AuditHashBadge.jsx  ← shows cryptographic hash badge (tamper-proof indicator)

      NotificationBell.jsx ← bell icon with unread count

  pages/

    auth/

      Login.jsx

      ForgotPassword.jsx

    user/

      ConsentBanner.jsx

      PreferenceCentre.jsx

      ConsentHistory.jsx

      DSARPortal.jsx

      DSARStatus.jsx

      GrievanceForm.jsx

      GrievanceStatus.jsx

      UserDashboard.jsx

      CookieConsent.jsx

    admin/

      AdminDashboard.jsx

      ConsentQueue.jsx

      DSARQueue.jsx

      GrievanceQueue.jsx

      BreachManagement.jsx

      AuditLogs.jsx

      UserRoleManagement.jsx

      DataRetentionPolicy.jsx

      NotificationCenter.jsx

      Reports.jsx

  App.jsx

  main.jsx

---

## MOCK DATA — src/api/index.js

// All mock data and API functions

// Replace each function with real fetch() calls when backend is ready

export const MOCK\_USER \= { id: 'u001', name: 'Priya Sharma', email: 'priya@example.com', language: 'en' };

export const MOCK\_ORG \= { id: 'org001', name: 'Example Fintech Pvt. Ltd.', grievanceOfficer: 'Amit Kumar', grievanceEmail: 'dpo@example.com' };

export const LANGUAGES \= \[

  'English','Hindi','Bengali','Telugu','Marathi','Tamil','Urdu',

  'Gujarati','Kannada','Odia','Malayalam','Punjabi','Assamese',

  'Maithili','Santali','Kashmiri','Nepali','Sindhi','Dogri',

  'Konkani','Manipuri','Bodo','Sanskrit'

\];

export const MOCK\_NOTICES \= \[

  { id: 'n1', purpose: 'Account Creation', category: 'Identity Data', retention\_days: 1825, mandatory: true, content: 'We collect your name, email, and phone to create your account.', data\_collected: 'Name, Email, Phone', user\_rights: 'You may access, correct or erase this data anytime.', withdrawal\_policy: 'Account will be deactivated upon withdrawal.' },

  { id: 'n2', purpose: 'Marketing Communications', category: 'Contact Data', retention\_days: 730, mandatory: false, content: 'We use your email to send product updates and offers.', data\_collected: 'Email address', user\_rights: 'You may withdraw consent anytime.', withdrawal\_policy: 'You will stop receiving marketing emails within 24 hours.' },

  { id: 'n3', purpose: 'Analytics', category: 'Usage Data', retention\_days: 365, mandatory: false, content: 'We use anonymized usage data to improve our product.', data\_collected: 'Usage patterns, device info', user\_rights: 'You may withdraw at any time.', withdrawal\_policy: 'Analytics tracking will stop immediately.' },

  { id: 'n4', purpose: 'KYC Verification', category: 'Sensitive Identity Data', retention\_days: 2555, mandatory: true, content: 'As required by RBI, we collect Aadhaar and PAN for identity verification.', data\_collected: 'Aadhaar number, PAN card', user\_rights: 'Required by law. Limited withdrawal rights.', withdrawal\_policy: 'May affect your ability to use financial services.' },

\];

export const MOCK\_CONSENTS \= \[

  { id: 'c1', user\_id: 'u001', notice\_id: 'n1', purpose: 'Account Creation', status: 'active', given\_at: '2026-01-15T10:00:00Z', expires\_at: '2031-01-15T10:00:00Z', language: 'en', version: '1.0', method: 'checkbox' },

  { id: 'c2', user\_id: 'u001', notice\_id: 'n2', purpose: 'Marketing Communications', status: 'withdrawn', given\_at: '2026-01-15T10:01:00Z', withdrawn\_at: '2026-05-01T09:00:00Z', language: 'en', version: '1.0', method: 'checkbox' },

  { id: 'c3', user\_id: 'u001', notice\_id: 'n3', purpose: 'Analytics', status: 'active', given\_at: '2026-01-15T10:02:00Z', expires\_at: '2027-01-15T10:02:00Z', language: 'en', version: '1.0', method: 'checkbox' },

  { id: 'c4', user\_id: 'u001', notice\_id: 'n4', purpose: 'KYC Verification', status: 'active', given\_at: '2026-01-15T10:03:00Z', expires\_at: '2033-01-15T10:03:00Z', language: 'en', version: '1.0', method: 'checkbox' },

\];

export const MOCK\_DSAR\_REQUESTS \= \[

  { id: 'd1', user\_id: 'u001', type: 'access', status: 'completed', submitted\_at: '2026-06-01T08:00:00Z', deadline\_at: '2026-07-01T08:00:00Z', resolved\_at: '2026-06-20T08:00:00Z', reference: 'DSAR-2026-001' },

  { id: 'd2', user\_id: 'u002', type: 'erase', status: 'in\_progress', submitted\_at: '2026-07-10T08:00:00Z', deadline\_at: '2026-08-09T08:00:00Z', reference: 'DSAR-2026-002' },

  { id: 'd3', user\_id: 'u003', type: 'correct', status: 'pending', submitted\_at: '2026-07-20T08:00:00Z', deadline\_at: '2026-08-19T08:00:00Z', reference: 'DSAR-2026-003' },

\];

export const MOCK\_GRIEVANCES \= \[

  { id: 'g1', user\_id: 'u001', category: 'Consent Violation', description: 'I withdrew marketing consent but still received emails.', status: 'in\_progress', submitted\_at: '2026-07-01T08:00:00Z', reference: 'GRV-2026-001', related\_dsar: null },

  { id: 'g2', user\_id: 'u002', category: 'Data Breach', description: 'I received a notification about my data being accessed without consent.', status: 'open', submitted\_at: '2026-07-15T08:00:00Z', reference: 'GRV-2026-002', related\_dsar: 'd1' },

\];

export const MOCK\_AUDIT\_LOGS \= \[

  { id: 'a1', log\_id: 'LOG-001', user\_id: 'u001', purpose\_id: 'n2', action\_type: 'withdraw', timestamp: '2026-05-01T09:00:00Z', consent\_status: 'withdrawn', initiator: 'user', source\_ip: '192.168.1.1', audit\_hash: 'sha256:abc123def456...' },

  { id: 'a2', log\_id: 'LOG-002', user\_id: 'u001', purpose\_id: 'n1', action\_type: 'grant', timestamp: '2026-01-15T10:00:00Z', consent\_status: 'active', initiator: 'user', source\_ip: '192.168.1.1', audit\_hash: 'sha256:xyz789ghi012...' },

  { id: 'a3', log\_id: 'LOG-003', user\_id: 'u002', purpose\_id: 'n4', action\_type: 'validate', timestamp: '2026-07-20T14:00:00Z', consent\_status: 'active', initiator: 'system', source\_ip: '10.0.0.1', audit\_hash: 'sha256:mno345pqr678...' },

\];

export const MOCK\_USERS\_ADMIN \= \[

  { id: 'u001', name: 'Priya Sharma', email: 'priya@example.com', role: 'data\_principal', created\_at: '2026-01-15' },

  { id: 'adm01', name: 'Amit Kumar', email: 'amit@example.com', role: 'admin', created\_at: '2025-12-01' },

  { id: 'aud01', name: 'Ravi Joshi', email: 'ravi@example.com', role: 'auditor', created\_at: '2025-12-01' },

  { id: 'grv01', name: 'Meena Patel', email: 'meena@example.com', role: 'grievance\_officer', created\_at: '2025-12-01' },

\];

export const MOCK\_RETENTION\_POLICIES \= \[

  { id: 'rp1', category: 'Identity Data', retention\_days: 1825, auto\_delete: true, exemption: 'Retain if RBI mandates', last\_purge: '2026-07-01' },

  { id: 'rp2', category: 'Marketing Data', retention\_days: 730, auto\_delete: true, exemption: null, last\_purge: '2026-06-15' },

\];

---

## SCREENS TO BUILD — COMPLETE LIST

---

### AUTH SCREENS

#### Login Page (`/login`)

- Centered card with logo "DataShield — DPDP Compliance"  
- Email \+ Password fields  
- Role selector: Data Principal / Admin / Auditor / Grievance Officer  
- "Sign In" button in Navy  
- Forgot password link  
- On success: redirect based on role (user → `/user/dashboard`, admin → `/admin/dashboard`)

#### Forgot Password (`/forgot-password`)

- Email input, "Send Reset Link" button  
- Confirmation message after submit

---

### USER SIDE SCREENS

#### User Dashboard (`/user/dashboard`)

**Header:** "Welcome, Priya" \+ language switcher \+ notification bell

**4 Quick-status cards:**

1. Active Consents count → links to Preference Centre  
2. Pending DSAR Requests → links to DSAR Status  
3. Open Grievances → links to Grievance Status  
4. Consents Expiring Soon (next 30 days) → amber color, links to Preference Centre

**Recent Activity section:** Last 5 actions (consent given/withdrawn, DSAR submitted) in a timeline format

**Quick Action buttons:** "Manage My Consents" | "Submit a Data Request" | "File a Complaint"

---

#### Consent Banner (`/consent-banner`)

**CRITICAL — This is what users see when they first visit a Data Fiduciary's platform**

- Header: "{Org Name} wants to use your data for the following purposes"  
- Language switcher top-right (English \+ all 22 scheduled languages)  
- Each purpose shown as an individual card with:  
  - Purpose name (bold)  
  - Plain-language description  
  - Data being collected  
  - Retention period (e.g., "Kept for 2 years")  
  - User rights summary  
  - Withdrawal policy ("What happens if you say no")  
  - MANDATORY badge (red) if legally required, OPTIONAL badge (grey) if not  
  - Individual toggle (ON/OFF) — default is OFF for all optional purposes  
  - Mandatory purposes shown as locked toggles with explanation  
- "Accept All Optional" and "Decline All Optional" shortcuts at top  
- No pre-checked toggles (DPDP Act requirement)  
- "Save My Choices" button at bottom  
- After save: inline success message \+ "View in Preference Centre" link  
- For users under 18: age-gate check → show Guardian Consent Flow instead

**Guardian Consent Flow (sub-page):**

- "Are you under 18?" toggle  
- If yes: show guardian email field \+ "Verify via DigiLocker" option  
- Guardian receives email with consent link  
- Guardian must actively click "I consent on behalf of my child"  
- Guardian identity verification step (OTP to guardian email)

---

#### Cookie Consent Banner (`/cookie-consent`)

**Shown as a bottom banner on first visit**

- "We use cookies on this website" header  
- Four category cards:  
  1. Essential Cookies (always ON, cannot toggle — locked with explanation)  
  2. Performance Cookies (toggle)  
  3. Analytics Cookies (toggle)  
  4. Marketing Cookies (toggle)  
- "Accept All" | "Decline All" | "Customize" buttons  
- "Customize" expands the category cards with toggles  
- "Save Preferences" button  
- Cookie Policy link  
- After save: banner dismisses, preferences logged with timestamp  
- "Later Visit" — small "Cookie Settings" link in footer to revisit  
- Auto-expiry notice: "Your preferences will be renewed in 12 months"

---

#### Preference Centre (`/user/preferences`)

**Where users manage all their existing consents**

- Header: "My Consent Preferences"  
- Filter tabs: All | Active | Withdrawn | Expiring Soon  
- Each consent as a card showing:  
  - Purpose name \+ category  
  - Current status badge (Active / Withdrawn / Expired)  
  - Date consent was given  
  - Expiry date (with amber warning if \<30 days)  
  - Toggle to withdraw/re-give consent  
  - "Consent History" link to see all past states for this purpose  
- Withdrawing shows ConfirmModal: "Are you sure? Here's what you'll lose: \[list\]"  
- After change: success toast \+ audit log entry created  
- "Download All My Consents" button (exports PDF/CSV)

---

#### Consent History (`/user/consent-history`)

**Detailed log of every consent action ever taken**

- Filter by: Purpose | Status | Date Range  
- Timeline view grouped by purpose:  
  - Each entry shows: action type (given/withdrawn/updated/renewed), timestamp, method (checkbox/OTP), version  
- Export button (PDF / CSV)  
- Search bar

---

#### DSAR Portal (`/user/dsar`)

**Data Subject Access Request — 3 distinct paths**

**Step 1 — Choose request type (3 large cards):**

- 📋 **Access My Data** — "Get a copy of all personal information we hold about you"  
- ✏️ **Correct My Data** — "Fix information that is wrong or incomplete"  
- 🗑️ **Erase My Data** — "Ask us to delete your personal information"

**Step 2 — Identity Verification:**

- "We'll send an OTP to your registered email/phone"  
- 6-digit OTP input field \+ resend link (60-second countdown)  
- Alternative: "Verify via DigiLocker" button

**Step 3 — Request Details:**

- For Access: no extra fields needed, just confirm  
- For Correct: "Which field?" dropdown \+ "Current value" \+ "Correct value" inputs \+ optional file upload  
- For Erase: optional reason dropdown \+ free text

**Step 4 — Submission Confirmation:**

- Green checkmark  
- Reference number (e.g., DSAR-2026-004)  
- "What happens next" timeline: Submitted → Under Review → In Progress → Completed  
- "Legal deadline: We will respond by \[deadline date\]"  
- Email confirmation sent notice  
- "Track My Request" button → DSARStatus page

---

#### DSAR Request Status (`/user/dsar/status`)

- List of all submitted requests with reference number, type, status, deadline  
- Click any request for full detail:  
  - Status tracker (step-by-step visual: Submitted → Verified → In Progress → Completed/Rejected)  
  - Deadline with SLA countdown (green/amber/red)  
  - If rejected: reason shown  
  - If completed: download data export link (for Access requests)  
  - If correction done: confirmation of what was corrected  
- Notification history for this request

---

#### Grievance Form (`/user/grievance`)

**Complaint submission for consent violations or data misuse**

- Complaint category dropdown:  
  - Consent Violation  
  - Data Breach  
  - Processing Error  
  - Unauthorized Data Sharing  
  - Delayed DSAR Response  
  - Other  
- "Related to a DSAR request?" toggle → if yes, show DSAR reference input  
- Description text area (minimum 50 characters)  
- Supporting evidence file upload (optional)  
- Language selector for submission  
- "Submit Complaint" button  
- After submit:  
  - Unique reference ID (e.g., GRV-2026-003)  
  - "We'll respond within \[X days\]"  
  - Email confirmation notice

---

#### Grievance Status (`/user/grievance/status`)

- List of all grievances with reference, category, status, submitted date  
- Click for detail:  
  - Status tracker: Submitted → Acknowledged → In Progress → Resolved / Escalated  
  - Resolution notes (when available)  
  - Escalation notice if unresolved beyond deadline  
  - "Provide Feedback on Resolution" button (once resolved) → star rating \+ text

---

### ADMIN SIDE SCREENS

#### Admin Dashboard (`/admin/dashboard`)

**The screen a DPO/compliance officer checks every morning**

**Top stats strip (6 cards):**

1. Total Active Consents  
2. Consents Withdrawn (this month)  
3. Open DSAR Requests  
4. DSAR Overdue (red badge)  
5. Open Grievances  
6. Consents Expiring in 30 days (amber)

**Charts:**

- Bar chart: DSAR requests by type (Access/Correct/Erase) — last 30 days  
- Line chart: Consents given vs withdrawn — last 6 months  
- Donut chart: Consent status distribution (Active/Withdrawn/Expired)

**"Needs Immediate Attention" section:**

- DSAR requests with \<5 days to deadline (red rows)  
- Grievances older than 10 days unresolved  
- Consents expiring in next 7 days

**Quick links:** DSAR Queue | Grievance Queue | Audit Logs | Retention Policy

---

#### DSAR Admin Queue (`/admin/dsar`)

**Main working screen for processing rights requests**

- Filter bar: All | Access | Correct | Erase | Pending | In Progress | Completed | Overdue  
- Search by reference number or user email  
- Sortable table columns:  
  - Reference Number  
  - Request Type (badge)  
  - User  
  - Submitted Date  
  - Legal Deadline \+ SLA Countdown (color-coded: green/amber/red/OVERDUE)  
  - Status badge  
  - "View" button

**Detail slide-in panel (opens on row click):**

- Full request details (type, user, submitted date, verification method used)  
- Identity verification status (OTP verified / DigiLocker verified)  
- For Access requests: "Prepare Data Export" button → triggers data collection  
- For Correct requests: show what correction was requested, "Mark Corrected" button  
- For Erase requests: "Initiate Erasure" button \+ confirmation modal  
- Status update dropdown (Pending → In Progress → Completed / Rejected)  
- Rejection reason field (shown if Rejected selected)  
- "Save & Notify User" button → updates status AND sends notification  
- Audit trail entries for this specific request (from audit\_log)  
- Legal exception toggle: "This request is exempt under law" with reason field

---

#### Consent Validation Queue (`/admin/consent-validation`)

**For Data Fiduciaries to validate consent before processing**

- Search by User ID \+ Purpose ID  
- Validation result: Valid / Invalid / Expired / Withdrawn  
- Shows: purpose alignment check, timestamp validity, consent status  
- API log view: all validation requests made today  
- Bulk validation option for batch processing

---

#### Grievance Queue (`/admin/grievances`)

- Filter: All | Open | In Progress | Resolved | Escalated  
- Table: Reference, User, Category, Days Open, Status, Grievance Officer assigned  
- Detail panel:  
  - Full complaint text  
  - Related DSAR reference (if any)  
  - Related consent records  
  - Resolution notes field (editable)  
  - Status update \+ "Notify User" button  
  - Escalation button: "Escalate to DPO"  
  - If escalated: DPO notified automatically, escalation logged  
  - Resolution closure: "Mark Resolved" \+ summary text → user notified \+ audit logged

---

#### Audit Logs (`/admin/audit`)

**Read-only — tamper-evident evidence trail**

- Header shows: "🔒 Immutable Audit Trail — No edits or deletions permitted"  
- Filter by: Action Type | User ID | Purpose ID | Date Range | Initiator  
- Table columns:  
  - Log ID  
  - Timestamp  
  - User ID  
  - Purpose ID  
  - Action Type (badge: grant/withdraw/update/validate/notification)  
  - Consent Status  
  - Initiator (user/system/Data Fiduciary)  
  - Source IP  
  - Audit Hash (truncated, click to copy full)  
- No edit/delete buttons anywhere on this screen  
- Export: PDF / CSV with digital signature for regulatory submission  
- Hash verification button: "Verify Log Integrity"

---

#### User Role Management (`/admin/roles`)

- User list with current roles  
- Add user button: name, email, role selector (Admin / DPO / Auditor / Grievance Officer)  
- Edit role button per user  
- Deactivate/Revoke access button (with confirmation modal)  
- Role permission matrix table (what each role can see/do)  
- MFA enforcement toggle per user  
- SSO configuration section  
- Role change audit log (every role change is logged)

---

#### Data Retention Policy (`/admin/retention`)

- Policy list per data category  
- Each policy shows: category, retention period (days), auto-delete on/off, exemption rule, last purge date  
- Edit policy form: retention period input, auto-delete toggle, exemption reason field, notification days (notify admin X days before purge)  
- "Run Manual Purge" button for a category → confirmation modal → logs result  
- Scheduled purge calendar view  
- Exemption management: data retained beyond schedule for legal reasons (with required reason field)  
- All purge activities logged in audit trail

---

#### Notification Center (`/admin/notifications`)

**Two sub-tabs:**

**User Notifications tab:**

- List of all notifications sent to users (consent confirmation, DSAR updates, renewal reminders, withdrawal confirmations)  
- Status: Delivered / Pending / Failed  
- Channel: Email / SMS / In-App  
- Retry failed notifications button  
- Template management: edit predefined notification templates for each scenario  
- Multi-language template editor (one template per language)

**Fiduciary/Processor Alerts tab:**

- Alerts sent to Data Fiduciaries (consent withdrawal, new consent, validation request)  
- API delivery status (200/500)  
- Escalation log: alerts unacknowledged beyond deadline → auto-escalated to DPO  
- "Test Alert" button for integration testing

---

#### Reports (`/admin/reports`)

**Export compliance-ready documents**

- Consent Report: all consents by date range, status, purpose → PDF/CSV  
- DSAR Completion Report: SLA compliance rate, average resolution time → PDF  
- Grievance Report: open/resolved/escalated by period → PDF  
- Audit Report: full audit trail export with digital signature → PDF (for regulator submission)  
- Retention Policy Report: purges run, data deleted, exemptions → PDF  
- Each report shows: generated timestamp, generated by (user ID), download link

---

### REUSABLE COMPONENTS — Must Build These

**StatusBadge.jsx** — variants: active (green), withdrawn (red), expired (red), pending (amber), in\_progress (blue), overdue (red flashing), resolved (green)

**ConfirmModal.jsx** — title, body text, consequence list (what the user will lose), confirm button (red for destructive, navy for neutral), cancel

**SLACountdown.jsx** — takes deadline\_at, shows "X days Y hours remaining", color: green (\>5 days), amber (2-5 days), red (\<2 days), "OVERDUE" if past deadline

**LanguageSwitcher.jsx** — dropdown with all 22 Eighth Schedule languages, selection stored in localStorage equivalent, applied to all user-facing text

**AuditHashBadge.jsx** — shows "🔒 SHA-256 verified" with truncated hash, click to copy

**NotificationBell.jsx** — bell icon \+ unread badge count, click opens notification dropdown with last 10 notifications

**ConsentCard.jsx** — reusable card for displaying a single consent with toggle, used in Preference Centre and Consent Banner

**TimelineTracker.jsx** — visual step tracker for DSAR/Grievance status (Submitted → Verified → In Progress → Completed)

---

## IMPORTANT RULES — Apply to Every Screen

1. **No pre-checked consent toggles anywhere** — DPDP Act requirement, always starts OFF for optional  
2. **Every destructive action** (withdraw consent, erase data, delete policy) needs ConfirmModal  
3. **Audit log entry must be shown** to admin after every state change  
4. **Mandatory consents** are shown with locked toggles and a "Required by law" explanation — never hidden  
5. **Language switcher** visible on every user-facing screen (consent, DSAR, grievance, cookie)  
6. **DSAR deadline** calculated as submitted\_at \+ 30 days, shown prominently everywhere  
7. **Guardian consent flow** triggers automatically when user age \< 18  
8. **Audit logs** have no edit/delete buttons — ever — and show "🔒 Immutable" banner  
9. **Grievance escalation** auto-triggers after \[configurable\] days unresolved  
10. **Processing exceptions** for legally-mandated data retention must be configurable in Retention Policy

---

## What NOT to Build Yet

- Real OTP sending (UI only, backend wires later)  
- Real DigiLocker integration (show button, placeholder response)  
- Real Bhashini API (language switcher UI ready, real translation backend wires later)  
- Real email/SMS notification delivery (UI shows "notification sent" with mock)  
- Payment/billing module

