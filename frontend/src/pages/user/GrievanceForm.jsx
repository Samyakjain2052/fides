// ============================================================================
// Grievance Form (/user/grievance)
// Complaint submission. Category, optional DSAR link, 50-char minimum
// description, optional evidence, language of submission.
// ============================================================================
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  getDSARRequests,
  GRIEVANCE_CATEGORIES,
  MOCK_ORG,
  MOCK_USER,
  submitGrievance,
} from "../../api";
import { useApp } from "../../context/AppContext";
import LanguageSwitcher from "../../components/common/LanguageSwitcher";

const MIN_CHARS = 50;
const RESPONSE_DAYS = 15;

export default function GrievanceForm() {
  const { language, notify } = useApp();
  const navigate = useNavigate();

  const [category, setCategory] = useState(GRIEVANCE_CATEGORIES[0]);
  const [linked, setLinked] = useState(false);
  const [relatedDsar, setRelatedDsar] = useState("");
  const [description, setDescription] = useState("");
  const [evidence, setEvidence] = useState("");
  const [myRequests, setMyRequests] = useState([]);
  const [created, setCreated] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getDSARRequests({ userId: MOCK_USER.id }).then(setMyRequests);
  }, []);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const row = await submitGrievance({
        category,
        description,
        relatedDsar: linked ? relatedDsar : null,
        language,
      });
      setCreated(row);
      notify("Your complaint has been filed.");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  if (created) {
    return (
      <div className="mx-auto max-w-2xl space-y-4">
        <div className="card border-success/40 bg-success/5 p-6 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-success/15 text-2xl text-success">
            ✓
          </div>
          <h1 className="font-semibold text-ink">Complaint received</h1>
          <p className="mt-1 text-sm text-muted">
            We&apos;ll respond within {RESPONSE_DAYS} days. If we don&apos;t, it is automatically
            escalated to the Data Protection Officer.
          </p>
          <p className="mt-3 inline-block rounded-lg border border-line bg-surface px-4 py-2 font-mono text-sm">
            {created.reference}
          </p>
        </div>

        <div className="card p-5 text-sm">
          <p className="font-semibold text-ink">What happens now</p>
          <ul className="mt-2 space-y-1.5 text-muted">
            <li>• {MOCK_ORG.grievanceOfficer} (Grievance Officer) is notified.</li>
            <li>• A confirmation email has been sent to {MOCK_USER.email}.</li>
            <li>• You can track the status and add feedback once it is resolved.</li>
          </ul>
          <div className="mt-4 flex flex-wrap gap-3">
            <button type="button" className="btn-primary" onClick={() => navigate("/user/grievance/status")}>
              Track my complaint
            </button>
            <Link to="/user/dashboard" className="btn-ghost">Back to dashboard</Link>
          </div>
        </div>
      </div>
    );
  }

  const remaining = Math.max(MIN_CHARS - description.length, 0);

  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">File a complaint</h1>
          <p className="text-sm text-muted">
            About a consent violation, a data misuse, or a late response to a request.
          </p>
        </div>
        <LanguageSwitcher />
      </div>

      <form onSubmit={submit} className="card space-y-4 p-5">
        <div>
          <label className="label" htmlFor="g-category">What is your complaint about?</label>
          <select id="g-category" className="input" value={category}
                  onChange={(e) => setCategory(e.target.value)}>
            {GRIEVANCE_CATEGORIES.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </div>

        <div className="rounded-lg border border-line p-3">
          <label className="flex items-center justify-between gap-3">
            <span className="text-sm text-ink">Is this related to a data request?</span>
            <button
              type="button"
              role="switch"
              aria-checked={linked}
              aria-label="Related to a data request"
              onClick={() => setLinked((v) => !v)}
              className={`relative inline-flex h-6 w-11 items-center rounded-full transition ${
                linked ? "bg-teal" : "bg-line"
              }`}
            >
              <span
                className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition ${
                  linked ? "translate-x-6" : "translate-x-1"
                }`}
              />
            </button>
          </label>

          {linked && (
            <div className="mt-3">
              <label className="label" htmlFor="g-dsar">DSAR reference</label>
              <input
                id="g-dsar"
                className="input"
                list="my-dsars"
                value={relatedDsar}
                onChange={(e) => setRelatedDsar(e.target.value)}
                placeholder="DSAR-2026-001"
              />
              <datalist id="my-dsars">
                {myRequests.map((r) => (
                  <option key={r.id} value={r.reference} />
                ))}
              </datalist>
            </div>
          )}
        </div>

        <div>
          <label className="label" htmlFor="g-description">Describe what happened</label>
          <textarea
            id="g-description"
            className="input min-h-[140px]"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Include dates, what you expected, and what actually happened."
            aria-describedby="g-count"
          />
          <p id="g-count" className={`mt-1 text-xs ${remaining > 0 ? "text-muted" : "text-success"}`}>
            {remaining > 0
              ? `${remaining} more characters needed (minimum ${MIN_CHARS})`
              : `${description.length} characters — thank you, that's enough detail`}
          </p>
        </div>

        <div>
          <label className="label" htmlFor="g-file">Supporting evidence (optional)</label>
          <input id="g-file" type="file" className="input"
                 onChange={(e) => setEvidence(e.target.files?.[0]?.name || "")} />
          {evidence && <p className="mt-1 text-xs text-muted">Attached: {evidence}</p>}
        </div>

        <div className="rounded-lg border border-line bg-canvas p-3 text-xs text-muted">
          Submitting in <strong className="text-ink">{language}</strong>. Your complaint goes to{" "}
          {MOCK_ORG.grievanceOfficer} ({MOCK_ORG.grievanceEmail}).
        </div>

        {error && (
          <p className="flex items-center gap-2 rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
            <span className="h-2 w-2 rounded-full bg-danger" aria-hidden="true" />
            {error}
          </p>
        )}

        <button type="submit" className="btn-primary w-full sm:w-auto"
                disabled={busy || description.length < MIN_CHARS}>
          {busy ? "Submitting…" : "Submit Complaint"}
        </button>
      </form>
    </div>
  );
}
