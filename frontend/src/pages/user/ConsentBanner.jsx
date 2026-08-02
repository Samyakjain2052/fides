// ============================================================================
// Consent Banner (/consent-banner)
// What a Data Principal sees on first visit. The DPDP-critical screen.
//
// Rules enforced here:
//   • NO pre-checked optional toggles — `choices` starts false for every
//     optional purpose, and there is no code path that defaults one to true.
//   • Mandatory purposes render as locked toggles WITH the reason shown
//     (ConsentCard handles this), never hidden.
//   • Language switcher is present on the screen itself, not just the chrome.
//   • Under-18 answers route into the guardian consent flow instead of saving.
// ============================================================================
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  getNotices,
  MOCK_ORG,
  saveConsentChoices,
  submitGuardianConsent,
  verifyOtp,
} from "../../api";
import { useApp } from "../../context/AppContext";
import ConsentCard from "../../components/common/ConsentCard";
import LanguageSwitcher from "../../components/common/LanguageSwitcher";
import { previewLock } from "../../config/modules";

export default function ConsentBanner() {
  const { t, language, notify } = useApp();
  const [notices, setNotices] = useState([]);
  const [choices, setChoices] = useState({});
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  // Age gate
  const [isMinor, setIsMinor] = useState(false);

  useEffect(() => {
    getNotices().then((rows) => {
      setNotices(rows);
      // Every optional purpose starts OFF. This is the requirement.
      const initial = {};
      rows.forEach((n) => {
        initial[n.id] = n.mandatory ? true : false;
      });
      setChoices(initial);
    });
  }, []);

  const optional = notices.filter((n) => !n.mandatory);

  const setAllOptional = (value) => {
    setChoices((prev) => {
      const next = { ...prev };
      optional.forEach((n) => {
        next[n.id] = value;
      });
      return next;
    });
  };

  const save = async () => {
    setBusy(true);
    try {
      await saveConsentChoices(choices, { language });
      setSaved(true);
      notify("Your choices have been saved and recorded in the audit trail.");
    } finally {
      setBusy(false);
    }
  };

  if (isMinor) {
    return <GuardianConsentFlow onBack={() => setIsMinor(false)} />;
  }

  return (
    <div className="mx-auto max-w-3xl space-y-5 pb-28">
      <div className="card p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-lg font-semibold text-ink">
              {MOCK_ORG.name} wants to use your data for the following purposes
            </h1>
            <p className="mt-1 text-sm text-muted">
              Nothing optional is switched on until you switch it on. You can change any of this
              later in your Preference Centre.
            </p>
          </div>
          <LanguageSwitcher />
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-line pt-4">
          <button type="button" className="btn-secondary" onClick={() => setAllOptional(true)}>
            {t("Accept All Optional")}
          </button>
          <button type="button" className="btn-ghost" onClick={() => setAllOptional(false)}>
            {t("Decline All Optional")}
          </button>
          <span className="text-xs text-muted">
            {optional.filter((n) => choices[n.id]).length} of {optional.length} optional purposes on
          </span>
        </div>
      </div>

      {/* Age gate — leads to the guardian flow rather than blocking. */}
      <div className="card flex flex-wrap items-center justify-between gap-3 p-4">
        <div>
          <p className="text-sm font-medium text-ink">Are you under 18?</p>
          <p className="text-xs text-muted">
            A parent or guardian must consent on your behalf, as the DPDP Act requires.
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={isMinor}
          aria-label="I am under 18"
          onClick={() => setIsMinor(true)}
          className="relative inline-flex h-6 w-11 items-center rounded-full bg-line"
        >
          <span className="inline-block h-4 w-4 translate-x-1 rounded-full bg-white shadow" />
        </button>
      </div>

      <div className="space-y-4">
        {notices.map((notice) => (
          <ConsentCard
            key={notice.id}
            notice={notice}
            checked={Boolean(choices[notice.id])}
            onChange={(value) => setChoices((prev) => ({ ...prev, [notice.id]: value }))}
            variant="banner"
          />
        ))}
      </div>

      {saved ? (
        <div className="card border-success/40 bg-success/5 p-5">
          <p className="flex items-center gap-2 font-medium text-ink">
            <span className="h-2.5 w-2.5 rounded-full bg-success" aria-hidden="true" />
            Your choices have been saved.
          </p>
          <p className="mt-1 text-sm text-muted">
            Recorded in {language} with a timestamp and an audit entry.
          </p>
          <Link to="/user/preferences" className="btn-secondary mt-4">
            View in Preference Centre
          </Link>
        </div>
      ) : (
        <div className="card sticky bottom-4 flex flex-wrap items-center justify-between gap-3 p-4 shadow-panel">
          <p className="text-xs text-muted">
            Mandatory purposes are required to hold an account and cannot be switched off.
          </p>
          <button type="button" className="btn-primary" onClick={save} disabled={busy} {...previewLock("consent", "Saving your choices")}>
            {busy ? "Saving…" : t("Save My Choices")}
          </button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Guardian Consent Flow (sub-page of the banner)
// guardian email → verify → guardian actively consents → OTP to guardian
// ---------------------------------------------------------------------------
function GuardianConsentFlow({ onBack }) {
  const { notify } = useApp();
  const [step, setStep] = useState(1);
  const [guardianEmail, setGuardianEmail] = useState("");
  const [childName, setChildName] = useState("");
  const [consented, setConsented] = useState(false);
  const [otp, setOtp] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const sendLink = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await submitGuardianConsent({ guardianEmail, childName });
      setStep(2);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const verify = async () => {
    setBusy(true);
    setError("");
    try {
      await verifyOtp(otp);
      setStep(4);
      notify("Guardian consent recorded.");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <div className="card p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold text-ink">Guardian consent required</h1>
            <p className="mt-1 text-sm text-muted">
              Because you are under 18, a parent or guardian has to give consent on your behalf.
            </p>
          </div>
          <LanguageSwitcher />
        </div>
        <button type="button" className="btn-ghost mt-3 px-0 text-sm" onClick={onBack}>
          ← I am over 18
        </button>
      </div>

      {step === 1 && (
        <form onSubmit={sendLink} className="card space-y-4 p-5">
          <p className="text-sm font-medium text-ink">Step 1 — Your guardian&apos;s details</p>
          <div>
            <label className="label" htmlFor="child-name">Your name</label>
            <input id="child-name" className="input" value={childName}
                   onChange={(e) => setChildName(e.target.value)} required />
          </div>
          <div>
            <label className="label" htmlFor="guardian-email">Guardian&apos;s email</label>
            <input id="guardian-email" type="email" className="input" value={guardianEmail}
                   onChange={(e) => setGuardianEmail(e.target.value)}
                   placeholder="guardian@example.com" required />
          </div>
          {error && <p className="text-sm text-danger">{error}</p>}
          <div className="flex flex-wrap gap-3">
            <button type="submit" className="btn-primary" disabled={busy} {...previewLock("consent", "Sending a guardian consent request")}>
              {busy ? "Sending…" : "Send consent request"}
            </button>
            <button type="button" className="btn-secondary" onClick={() => setStep(2)}>
              Verify guardian via DigiLocker
            </button>
          </div>
          <p className="text-xs text-muted">
            DigiLocker verification is a placeholder in this build — no real call is made.
          </p>
        </form>
      )}

      {step === 2 && (
        <div className="card space-y-4 p-5">
          <p className="text-sm font-medium text-ink">Step 2 — Guardian receives the request</p>
          <div className="rounded-lg border border-line bg-canvas p-4 text-sm">
            <p className="text-muted">To: {guardianEmail || "guardian@example.com"}</p>
            <p className="mt-2 text-ink">
              {childName || "Your child"} has asked to use {MOCK_ORG.name}. As their guardian, you
              must actively consent before any optional processing begins.
            </p>
          </div>
          <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-line p-3">
            <input type="checkbox" className="mt-1" checked={consented}
                   onChange={(e) => setConsented(e.target.checked)} />
            <span className="text-sm text-ink">
              I consent on behalf of my child.
              <span className="mt-0.5 block text-xs text-muted">
                This has to be an active choice — it is never pre-ticked.
              </span>
            </span>
          </label>
          <button type="button" className="btn-primary" disabled={!consented}
                  onClick={() => setStep(3)}>
            Continue to identity verification
          </button>
        </div>
      )}

      {step === 3 && (
        <div className="card space-y-4 p-5">
          <p className="text-sm font-medium text-ink">Step 3 — Verify guardian identity</p>
          <p className="text-sm text-muted">
            We sent a 6-digit code to {guardianEmail || "the guardian's email"}.
          </p>
          <input className="input max-w-[12rem] tracking-[0.4em]" value={otp} inputMode="numeric"
                 maxLength={6} placeholder="000000"
                 onChange={(e) => setOtp(e.target.value.replace(/\D/g, ""))}
                 aria-label="Six digit code" />
          {error && <p className="text-sm text-danger">{error}</p>}
          <button type="button" className="btn-primary" onClick={verify} disabled={busy} {...previewLock("consent", "Recording consent")}>
            {busy ? "Verifying…" : "Verify and record consent"}
          </button>
        </div>
      )}

      {step === 4 && (
        <div className="card border-success/40 bg-success/5 p-5">
          <p className="flex items-center gap-2 font-medium text-ink">
            <span className="h-2.5 w-2.5 rounded-full bg-success" aria-hidden="true" />
            Guardian consent recorded
          </p>
          <p className="mt-1 text-sm text-muted">
            The consent is stored against the guardian&apos;s verified identity, with an audit entry.
          </p>
          <Link to="/user/preferences" className="btn-secondary mt-4">
            View in Preference Centre
          </Link>
        </div>
      )}
    </div>
  );
}
