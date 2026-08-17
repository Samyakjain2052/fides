// ============================================================================
// Grievance Form (/user/grievance) — DPDP §13
//
// Real submission against /v1/grievances.
//
// Two things removed rather than wired:
//
//   * The "Supporting evidence (optional)" file input. It read a filename and
//     threw the file away. Offering somebody an upload for the proof behind a
//     statutory complaint, and silently discarding it, is worse than not offering
//     one — they believe they have submitted evidence they have not.
//   * The 50-character minimum, which was stricter than the server's and enforced
//     nowhere else. The server asks for a sentence; the counter now reflects that
//     rather than inventing its own threshold.
//
// The Grievance Officer and the response deadline are read from the API, not from
// a mock constant. They are per-tenant and statutory — a hardcoded "15 days" is
// wrong for every customer who promised faster.
// ============================================================================
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { CATEGORIES, fileGrievance, officer as fetchOfficer } from "../../api/grievances";
import { myRows as myDsarRows } from "../../api/dsar";
import { useApp } from "../../context/AppContext";

const MIN_CHARS = 10;

export default function GrievanceForm() {
  const { notify } = useApp();
  const navigate = useNavigate();

  const [category, setCategory] = useState(CATEGORIES[0].id);
  const [linked, setLinked] = useState(false);
  const [relatedDsarId, setRelatedDsarId] = useState("");
  const [description, setDescription] = useState("");
  const [myRequests, setMyRequests] = useState([]);
  const [officer, setOfficer] = useState(null);
  const [created, setCreated] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    myDsarRows().then(setMyRequests).catch(() => setMyRequests([]));
    fetchOfficer().then(setOfficer).catch(() => setOfficer(null));
  }, []);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const row = await fileGrievance({
        category,
        description,
        relatedDsarId: linked ? relatedDsarId : null,
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
    const deadline = new Date(created.deadline_at).toLocaleDateString();
    return (
      <div className="mx-auto max-w-2xl space-y-4">
        <div className="card border-success/40 bg-success/5 p-6 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-success/15 text-2xl text-success">
            ✓
          </div>
          <h1 className="font-semibold text-ink">Complaint received</h1>
          <p className="mt-1 text-sm text-muted">
            We must respond by <strong className="text-ink">{deadline}</strong>.
          </p>
          <p className="mt-3 inline-block rounded-lg border border-line bg-surface px-4 py-2 font-mono text-sm">
            {created.reference}
          </p>
        </div>

        <div className="card p-5 text-sm">
          <p className="font-semibold text-ink">What happens now</p>
          <ul className="mt-2 space-y-1.5 text-muted">
            <li>• A confirmation has been emailed to you.</li>
            <li>
              {/* The real threshold, from the tenant. Not "10 days" hardcoded. */}
              • If it is not resolved within{" "}
              {officer ? `${officer.escalation_days} days` : "the escalation window"}
              , it is escalated to the Grievance Officer automatically.
            </li>
            <li>
              • Once resolved you can rate the outcome. A poor rating reopens the
              complaint rather than just recording a score.
            </li>
            <li>
              • If we cannot resolve it to your satisfaction, you may approach the
              Data Protection Board of India.
            </li>
          </ul>
          <div className="mt-4 flex flex-wrap gap-3">
            <button
              type="button"
              className="btn-primary"
              onClick={() => navigate("/user/grievance/status")}
            >
              Track my complaint
            </button>
            <Link to="/user/dashboard" className="btn-ghost">
              Back to dashboard
            </Link>
          </div>
        </div>
      </div>
    );
  }

  const remaining = Math.max(MIN_CHARS - description.trim().length, 0);

  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">File a complaint</h1>
        <p className="text-sm text-muted">
          About a consent that was ignored, data that is wrong, or a request we
          did not answer in time.
        </p>
      </div>

      <form onSubmit={submit} className="card space-y-4 p-5">
        <div>
          <label className="label" htmlFor="g-category">
            What is your complaint about?
          </label>
          <select
            id="g-category"
            className="input"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          >
            {CATEGORIES.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label}
              </option>
            ))}
          </select>
        </div>

        <div className="rounded-lg border border-line p-3">
          <label className="flex items-center justify-between gap-3">
            <span className="text-sm text-ink">Is this about a data request?</span>
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
              <label className="label" htmlFor="g-dsar">
                Which request?
              </label>
              {/* A select over the caller's own requests, not a free-text
                  reference. The API takes an id, and asking somebody to retype a
                  reference they can be shown is a way to get it wrong. */}
              <select
                id="g-dsar"
                className="input"
                value={relatedDsarId}
                onChange={(e) => setRelatedDsarId(e.target.value)}
              >
                <option value="">Select a request…</option>
                {myRequests.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.reference} — {r.type}
                  </option>
                ))}
              </select>
              {myRequests.length === 0 && (
                <p className="mt-1 text-xs text-muted">
                  You have no data requests on record.
                </p>
              )}
            </div>
          )}
        </div>

        <div>
          <label className="label" htmlFor="g-description">
            Describe what happened
          </label>
          <textarea
            id="g-description"
            className="input min-h-[160px]"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            maxLength={8000}
            placeholder="Include dates, what you expected, and what actually happened."
            aria-describedby="g-count"
          />
          <p
            id="g-count"
            className={`mt-1 text-xs ${remaining > 0 ? "text-muted" : "text-success"}`}
          >
            {remaining > 0
              ? `Please write at least a sentence (${remaining} more characters).`
              : `${description.trim().length} characters.`}
          </p>
        </div>

        {officer && (
          <div className="rounded-lg border border-line bg-canvas p-3 text-xs text-muted">
            {officer.published ? (
              <>
                This complaint goes to{" "}
                <strong className="text-ink">{officer.name}</strong>, Grievance
                Officer ({officer.email}). We must respond within{" "}
                {officer.sla_days} days.
              </>
            ) : (
              // §13 requires a published officer. Saying so beats a blank line
              // where a statutory contact belongs.
              <>
                This organisation has not published a Grievance Officer. Your
                complaint will still be recorded and is still subject to the{" "}
                {officer.sla_days}-day response deadline.
              </>
            )}
          </div>
        )}

        {error && (
          <p className="flex items-center gap-2 rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
            <span className="h-2 w-2 shrink-0 rounded-full bg-danger" aria-hidden="true" />
            {error}
          </p>
        )}

        <button
          type="submit"
          className="btn-primary w-full sm:w-auto"
          disabled={busy || remaining > 0 || (linked && !relatedDsarId)}
        >
          {busy ? "Submitting…" : "Submit complaint"}
        </button>
      </form>
    </div>
  );
}
