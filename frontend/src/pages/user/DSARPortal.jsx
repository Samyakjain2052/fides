// ============================================================================
// DSAR Portal (/user/dsar) — the four-step rights request.
//   1. choose type (access / correct / erase)
//   2. verify identity (OTP with a 60s resend countdown, or DigiLocker)
//   3. request details (different per type)
//   4. confirmation with reference number, deadline and next steps
// ============================================================================
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  CORRECTABLE_FIELDS,
  ERASURE_REASONS,
  sendOtp,
  submitDSAR,
  verifyDigiLocker,
  verifyOtp,
} from "../../api";
import { useApp } from "../../context/AppContext";
import LanguageSwitcher from "../../components/common/LanguageSwitcher";
import TimelineTracker, { DSAR_STEPS } from "../../components/common/TimelineTracker";

const TYPES = [
  {
    id: "access",
    icon: "📋",
    title: "Access My Data",
    blurb: "Get a copy of all personal information we hold about you.",
  },
  {
    id: "correct",
    icon: "✏️",
    title: "Correct My Data",
    blurb: "Fix information that is wrong or incomplete.",
  },
  {
    id: "erase",
    icon: "🗑️",
    title: "Erase My Data",
    blurb: "Ask us to delete your personal information.",
  },
];

export default function DSARPortal() {
  const { t, notify } = useApp();
  const navigate = useNavigate();

  const [step, setStep] = useState(1);
  const [type, setType] = useState(null);
  const [verification, setVerification] = useState(null);
  const [created, setCreated] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // OTP state
  const [otp, setOtp] = useState("");
  const [cooldown, setCooldown] = useState(0);

  // details
  const [correction, setCorrection] = useState({ field: CORRECTABLE_FIELDS[0], current: "", corrected: "", file: "" });
  const [erasure, setErasure] = useState({ reason: ERASURE_REASONS[0], notes: "" });

  useEffect(() => {
    if (cooldown <= 0) return undefined;
    const id = setTimeout(() => setCooldown((c) => c - 1), 1000);
    return () => clearTimeout(id);
  }, [cooldown]);

  const chooseType = async (id) => {
    setType(id);
    setStep(2);
    await sendOtp("your registered email and phone");
    setCooldown(60);
  };

  const resend = async () => {
    await sendOtp("your registered email and phone");
    setCooldown(60);
    notify("A new code has been sent.", "info");
  };

  const doVerifyOtp = async () => {
    setBusy(true);
    setError("");
    try {
      const res = await verifyOtp(otp);
      setVerification(res.method);
      setStep(3);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const doVerifyDigiLocker = async () => {
    setBusy(true);
    setError("");
    try {
      const res = await verifyDigiLocker();
      setVerification(res.method);
      setStep(3);
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      const details =
        type === "correct" ? { correction } : type === "erase" ? { erasure } : {};
      const row = await submitDSAR({ type, verification, details });
      setCreated(row);
      setStep(4);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const chosen = TYPES.find((x) => x.id === type);

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">Submit a data request</h1>
          <p className="text-sm text-muted">
            Your rights under the DPDP Act, 2023. We must respond within 30 days.
          </p>
        </div>
        <LanguageSwitcher />
      </div>

      {/* Step rail */}
      <ol className="card flex flex-wrap gap-x-6 gap-y-2 p-4 text-sm">
        {["Choose request", "Verify identity", "Details", "Confirmation"].map((label, i) => {
          const n = i + 1;
          const done = step > n;
          const current = step === n;
          return (
            <li key={label} className="flex items-center gap-2">
              <span
                className={`flex h-6 w-6 items-center justify-center rounded-full text-xs font-semibold ${
                  done ? "bg-success text-white" : current ? "bg-navy text-white" : "bg-line text-muted"
                }`}
              >
                {done ? "✓" : n}
              </span>
              <span className={current ? "font-semibold text-ink" : "text-muted"}>{label}</span>
            </li>
          );
        })}
      </ol>

      {/* ---------------------------------------------------------- step 1 -- */}
      {step === 1 && (
        <div className="grid gap-4 sm:grid-cols-3">
          {TYPES.map((x) => (
            <button
              key={x.id}
              type="button"
              onClick={() => chooseType(x.id)}
              className="card p-5 text-left transition hover:border-navy/40 hover:shadow-md"
            >
              <span className="text-2xl" aria-hidden="true">{x.icon}</span>
              <p className="mt-3 font-semibold text-ink">{t(x.title)}</p>
              <p className="mt-1 text-sm text-muted">{x.blurb}</p>
              <span className="mt-4 inline-block text-sm text-teal underline">Start →</span>
            </button>
          ))}
        </div>
      )}

      {/* ---------------------------------------------------------- step 2 -- */}
      {step === 2 && (
        <div className="card space-y-4 p-5">
          <div>
            <p className="text-sm font-semibold text-ink">Verify it&apos;s really you</p>
            <p className="mt-1 text-sm text-muted">
              We&apos;ll send a one-time code to your registered email and phone. This protects your
              data from someone else requesting it.
            </p>
          </div>

          <div>
            <label className="label" htmlFor="otp">6-digit code</label>
            <input
              id="otp"
              className="input max-w-[12rem] text-lg tracking-[0.4em]"
              value={otp}
              onChange={(e) => setOtp(e.target.value.replace(/\D/g, ""))}
              inputMode="numeric"
              maxLength={6}
              placeholder="000000"
            />
            <div className="mt-2 text-xs text-muted">
              {cooldown > 0 ? (
                <span>Resend available in {cooldown}s</span>
              ) : (
                <button type="button" className="text-teal underline" onClick={resend}>
                  Resend code
                </button>
              )}
            </div>
          </div>

          {error && <p className="text-sm text-danger">{error}</p>}

          <div className="flex flex-wrap gap-3">
            <button type="button" className="btn-primary" onClick={doVerifyOtp} disabled={busy}>
              {busy ? "Verifying…" : "Verify code"}
            </button>
            <button type="button" className="btn-secondary" onClick={doVerifyDigiLocker} disabled={busy}>
              Verify via DigiLocker
            </button>
            <button type="button" className="btn-ghost" onClick={() => setStep(1)}>
              Back
            </button>
          </div>
          <p className="text-xs text-muted">
            Any 6 digits work in this build; DigiLocker returns a placeholder response.
          </p>
        </div>
      )}

      {/* ---------------------------------------------------------- step 3 -- */}
      {step === 3 && (
        <div className="card space-y-4 p-5">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-semibold text-ink">{chosen?.title}</p>
            <span className="tag">identity verified via {verification}</span>
          </div>

          {type === "access" && (
            <p className="text-sm text-muted">
              Nothing else is needed. When you submit, we will collect every piece of personal data
              we hold about you and make it available as a download.
            </p>
          )}

          {type === "correct" && (
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="sm:col-span-2">
                <label className="label" htmlFor="c-field">Which field is wrong?</label>
                <select
                  id="c-field"
                  className="input"
                  value={correction.field}
                  onChange={(e) => setCorrection({ ...correction, field: e.target.value })}
                >
                  {CORRECTABLE_FIELDS.map((f) => (
                    <option key={f} value={f}>{f}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="label" htmlFor="c-current">Current value</label>
                <input id="c-current" className="input" value={correction.current}
                       onChange={(e) => setCorrection({ ...correction, current: e.target.value })} />
              </div>
              <div>
                <label className="label" htmlFor="c-new">Correct value</label>
                <input id="c-new" className="input" value={correction.corrected}
                       onChange={(e) => setCorrection({ ...correction, corrected: e.target.value })} />
              </div>
              <div className="sm:col-span-2">
                <label className="label" htmlFor="c-file">Supporting document (optional)</label>
                <input id="c-file" type="file" className="input"
                       onChange={(e) => setCorrection({ ...correction, file: e.target.files?.[0]?.name || "" })} />
              </div>
            </div>
          )}

          {type === "erase" && (
            <div className="space-y-3">
              <div>
                <label className="label" htmlFor="e-reason">Reason (optional)</label>
                <select id="e-reason" className="input" value={erasure.reason}
                        onChange={(e) => setErasure({ ...erasure, reason: e.target.value })}>
                  {ERASURE_REASONS.map((r) => (
                    <option key={r} value={r}>{r}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="label" htmlFor="e-notes">Anything else you want to tell us</label>
                <textarea id="e-notes" className="input min-h-[90px]" value={erasure.notes}
                          onChange={(e) => setErasure({ ...erasure, notes: e.target.value })} />
              </div>
              <p className="rounded-lg border border-warning/40 bg-warning/5 p-3 text-xs text-ink">
                <strong>Note:</strong> data we are legally required to keep (for example KYC records
                under RBI rules) may be retained even after an erasure request. We will tell you
                exactly what was kept and why.
              </p>
            </div>
          )}

          {error && <p className="text-sm text-danger">{error}</p>}

          <div className="flex flex-wrap gap-3">
            <button type="button" className="btn-primary" onClick={submit} disabled={busy}>
              {busy ? "Submitting…" : "Submit request"}
            </button>
            <button type="button" className="btn-ghost" onClick={() => setStep(2)}>
              Back
            </button>
          </div>
        </div>
      )}

      {/* ---------------------------------------------------------- step 4 -- */}
      {step === 4 && created && (
        <div className="space-y-4">
          <div className="card border-success/40 bg-success/5 p-6 text-center">
            <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-success/15 text-2xl text-success">
              ✓
            </div>
            <h2 className="font-semibold text-ink">Request submitted</h2>
            <p className="mt-1 text-sm text-muted">Keep this reference number for your records.</p>
            <p className="mt-3 inline-block rounded-lg border border-line bg-surface px-4 py-2 font-mono text-sm">
              {created.reference}
            </p>
          </div>

          <div className="card p-5">
            <p className="text-sm font-semibold text-ink">What happens next</p>
            <div className="mt-4">
              <TimelineTracker steps={DSAR_STEPS} status={created.status} />
            </div>
            <div className="mt-4 rounded-lg border border-line bg-canvas p-3 text-sm">
              <p className="text-ink">
                <strong>Legal deadline:</strong> we will respond by{" "}
                {new Date(created.deadline_at).toLocaleDateString()} (30 days from submission).
              </p>
              <p className="mt-1 text-xs text-muted">
                A confirmation email has been sent to your registered address.
              </p>
            </div>
            <div className="mt-4 flex flex-wrap gap-3">
              <button type="button" className="btn-primary" onClick={() => navigate("/user/dsar/status")}>
                Track My Request
              </button>
              <Link to="/user/dashboard" className="btn-ghost">Back to dashboard</Link>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
