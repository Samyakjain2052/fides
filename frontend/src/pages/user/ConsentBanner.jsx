// ============================================================================
// Consent Banner (/consent-banner)
// What a Data Principal sees on first visit. The DPDP-critical screen.
//
// Real as of the publishable-key work: purposes, notice wording and collection
// all come from /public/v1/banner/*, backed by PostgreSQL. See
// docs/PUBLISHABLE_KEY_SECURITY.md for why this page can safely carry a key.
//
// Rules enforced here:
//   • NO pre-checked toggles. `choices` starts false for every purpose and
//     there is no code path that defaults one to true.
//   • Declining means NOT COLLECTING consent — never a withdrawal. A
//     publishable key cannot withdraw, and this screen must not appear to
//     offer something it cannot do.
//   • The wording shown is the server's published notice text, because that is
//     the text the recorded consent is versioned against.
//   • Language switcher on the screen itself, not just the chrome.
//   • Under-18 routes to the guardian flow, which is still preview — see below.
// ============================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  bannerOrganisation,
  bannerPurposes,
  collectConsent,
  newIdempotencyKey,
  principalRef,
} from "../../api/banner";
import { submitGuardianConsent, verifyOtp } from "../../api";
import { useApp } from "../../context/AppContext";
import ConsentCard from "../../components/common/ConsentCard";
import LanguageSwitcher from "../../components/common/LanguageSwitcher";
import { previewLock } from "../../config/modules";

export default function ConsentBanner() {
  const { t, language, notify, user } = useApp();
  const [purposes, setPurposes] = useState([]);
  const [orgName, setOrgName] = useState("");
  const [choices, setChoices] = useState({});
  const [receipts, setReceipts] = useState([]);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Age gate
  const [isMinor, setIsMinor] = useState(false);

  // One key per mount. A double-click, or a retry after a flaky network,
  // replays the first response instead of recording a second consent.
  const idempotencyKeys = useMemo(() => new Map(), []);

  const load = useCallback(async () => {
    try {
      const rows = await bannerPurposes();
      setPurposes(rows);
      // Fetched alongside, and allowed to fail on its own. Not knowing the
      // organisation's name is a reason to word the heading generically, not a
      // reason to refuse to show the banner — and certainly not a reason to
      // print somebody else's name, which is what the hardcoded value did.
      bannerOrganisation()
        .then((o) => setOrgName(o?.name || ""))
        .catch(() => setOrgName(""));
      // Every purpose starts OFF. This is the requirement, and the reason the
      // initial state is built from the response rather than defaulted anywhere.
      setChoices(Object.fromEntries(rows.map((r) => [r.key, false])));
      setError(null);
    } catch (e) {
      setError(e.message || "Could not load the consent options.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const setAllOptional = (value) =>
    setChoices((prev) => Object.fromEntries(Object.keys(prev).map((k) => [k, value])));

  const chosen = Object.entries(choices).filter(([, on]) => on).map(([k]) => k);

  const save = async () => {
    setBusy(true);
    const ref = principalRef(user);
    const collected = [];
    const failures = [];

    // One call per granted purpose. A declined purpose produces NO call at all:
    // "no" means no consent was collected, not that an existing one was
    // withdrawn — and this credential cannot withdraw anything.
    for (const key of chosen) {
      if (!idempotencyKeys.has(key)) idempotencyKeys.set(key, newIdempotencyKey());
      try {
        const out = await collectConsent({
          principalRef: ref,
          purpose: key,
          language,
          source: "consent-banner",
          idempotencyKey: idempotencyKeys.get(key),
        });
        collected.push(out);
      } catch (e) {
        failures.push(`${key}: ${e.message}`);
      }
    }

    setBusy(false);

    if (failures.length) {
      // Say which ones and why. A banner that silently records some choices and
      // drops others is worse than one that fails outright, because nobody
      // finds out until it matters.
      notify(`Some choices could not be recorded — ${failures.join("; ")}`, "error");
      if (!collected.length) return;
    }

    setReceipts(collected);
    setSaved(true);
    notify(
      collected.length
        ? "Your choices have been recorded, with a receipt."
        : "No optional purposes were switched on, so nothing was recorded."
    );
  };

  if (isMinor) {
    return <GuardianConsentFlow orgName={orgName} onBack={() => setIsMinor(false)} />;
  }

  if (loading) {
    return <p className="mx-auto max-w-3xl text-sm text-muted">Loading your options…</p>;
  }

  if (error) {
    // An honest dead end beats a form that looks alive and records nothing.
    return (
      <div className="mx-auto max-w-3xl">
        <div className="card border-danger/40 bg-danger/5 p-5">
          <p className="font-medium text-ink">This consent banner is not available</p>
          <p className="mt-1 text-sm text-muted">{error}</p>
          <button type="button" className="btn-secondary mt-4" onClick={load}>
            Try again
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl space-y-5 pb-28">
      <div className="card p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-lg font-semibold text-ink">
              {orgName || "This organisation"} would like to use your data for the
              following purposes
            </h1>
            <p className="mt-1 text-sm text-muted">
              Nothing here is switched on until you switch it on. You can change any
              of it later in your Preference Centre.
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
            {chosen.length} of {purposes.length} switched on
          </span>
        </div>
      </div>

      {/* Mandatory purposes are NOT offered here — they do not rest on consent,
          and a toggle for one would be a dark pattern. Saying nothing at all
          would be misleading by omission, so say it plainly instead. */}
      <div className="card border-info/40 bg-info/5 p-4">
        <p className="text-sm text-ink">
          Some processing happens on other legal bases — a statutory obligation, for
          example — and is not offered as a choice here because it is not consent.
        </p>
        <Link to="/user/preferences" className="mt-1 inline-block text-sm text-teal underline">
          See everything held about you, and why
        </Link>
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
        {purposes.length === 0 && (
          <p className="card p-6 text-center text-sm text-muted">
            This organisation has no purposes that ask for consent.
          </p>
        )}
        {purposes.map((p) => (
          <ConsentCard
            key={p.key}
            notice={{
              purpose: p.name,
              category: p.category,
              mandatory: false,
              retention_days: p.retention_days,
              content: p.content,
              data_collected: p.data_collected,
              user_rights: p.user_rights,
              withdrawal_policy: p.withdrawal_policy,
            }}
            consent={{ status: "never_given", version: p.notice_version, language: p.language }}
            checked={Boolean(choices[p.key])}
            onChange={(value) => setChoices((prev) => ({ ...prev, [p.key]: value }))}
            variant="banner"
          />
        ))}
      </div>

      {saved ? (
        <div className="card border-success/40 bg-success/5 p-5">
          <p className="flex items-center gap-2 font-medium text-ink">
            <span className="h-2.5 w-2.5 rounded-full bg-success" aria-hidden="true" />
            {receipts.length
              ? "Your choices have been recorded."
              : "Nothing was switched on, so nothing was recorded."}
          </p>
          {receipts.length > 0 && (
            <>
              <p className="mt-1 text-sm text-muted">
                Recorded in {receipts[0].language}, against notice version{" "}
                {receipts[0].notice_version}, with an entry in the audit trail.
              </p>
              {/* The receipt id is the person's handle if they ever dispute the
                  record. Showing it is the difference between a claim and a
                  receipt. */}
              <ul className="mt-3 space-y-1">
                {receipts.map((r) => (
                  <li key={r.server_receipt_id} className="font-mono text-xs text-muted">
                    {r.purpose} — {r.server_receipt_id}
                  </li>
                ))}
              </ul>
            </>
          )}
          <Link to="/user/preferences" className="btn-secondary mt-4">
            View in Preference Centre
          </Link>
        </div>
      ) : (
        <div className="card sticky bottom-4 flex flex-wrap items-center justify-between gap-3 p-4 shadow-panel">
          <p className="text-xs text-muted">
            Switching something off simply means no consent is recorded for it.
          </p>
          <button type="button" className="btn-primary" onClick={save} disabled={busy}>
            {busy ? "Recording…" : t("Save My Choices")}
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
// `orgName` is passed down rather than re-fetched: this is the same banner
// session, and asking the server twice for one string on a screen that is
// already declared preview would be noise.
function GuardianConsentFlow({ onBack, orgName }) {
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
            <button type="submit" className="btn-primary" disabled={busy} {...previewLock("consent_guardian", "Sending a guardian consent request")}>
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
              {childName || "Your child"} has asked to use{" "}
              {orgName || "this service"}. As their guardian, you
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
          <button type="button" className="btn-primary" onClick={verify} disabled={busy} {...previewLock("consent_guardian", "Recording consent")}>
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
