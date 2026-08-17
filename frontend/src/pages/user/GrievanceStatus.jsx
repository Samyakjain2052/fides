// ============================================================================
// Grievance Status (/user/grievance/status)
//
// Real data from /v1/grievances/mine.
//
// The overdue calculation now comes from the server's `is_overdue`, which is
// computed against the statutory deadline on every read. The previous version
// derived it in the browser from a hardcoded escalation constant — so it was
// wrong for any customer with a different SLA, and told the person their
// complaint was fine when it was not.
//
// The rating is the one control here that does something. 1 or 2 reopens the
// complaint, and the UI says so before it is submitted: a person should know that
// "dissatisfied" means "look at this again", not "note my displeasure".
// ============================================================================
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  CATEGORY_LABEL,
  myGrievances,
  officer as fetchOfficer,
  rateResolution,
  STATUS_LABEL,
} from "../../api/grievances";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";
import TimelineTracker, { GRIEVANCE_STEPS } from "../../components/common/TimelineTracker";

export default function GrievanceStatus() {
  const { notify } = useApp();
  const [rows, setRows] = useState([]);
  const [selected, setSelected] = useState(null);
  const [officer, setOfficer] = useState(null);
  const [rating, setRating] = useState(0);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await myGrievances();
      setRows(r);
      setSelected((prev) =>
        (prev ? r.find((x) => x.id === prev.id) || r[0] : r[0]) || null,
      );
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
    fetchOfficer().then(setOfficer).catch(() => setOfficer(null));
  }, [load]);

  const sendFeedback = async () => {
    setBusy(true);
    setError("");
    try {
      await rateResolution(selected.id, { rating, comment });
      await load();
      notify(
        rating <= 2
          ? "Recorded — your complaint has been reopened."
          : "Thanks — your feedback has been recorded.",
      );
      setRating(0);
      setComment("");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const fmt = (iso) => (iso ? new Date(iso).toLocaleDateString() : "—");

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">My complaints</h1>
        <p className="text-sm text-muted">
          Every complaint you have filed, its statutory deadline, and how it was
          resolved.
        </p>
      </div>

      {error && (
        <div className="rounded-lg border border-danger/50 bg-danger/10 p-3 text-sm text-ink">
          {error}
        </div>
      )}

      {rows.length === 0 ? (
        <div className="card p-8 text-center">
          <p className="text-sm text-muted">You have not filed any complaints.</p>
          <Link to="/user/grievance" className="btn-primary mt-4 inline-block">
            File a complaint
          </Link>
        </div>
      ) : (
        <div className="grid gap-5 lg:grid-cols-[18rem_1fr]">
          {/* ------------------------------------------------------- list -- */}
          <ul className="space-y-2">
            {rows.map((g) => (
              <li key={g.id}>
                <button
                  type="button"
                  onClick={() => setSelected(g)}
                  className={`w-full rounded-xl border p-3 text-left transition ${
                    selected?.id === g.id
                      ? "border-teal bg-teal/5"
                      : "border-line bg-surface hover:bg-line/30"
                  }`}
                >
                  <p className="font-mono text-xs text-muted">{g.reference}</p>
                  <p className="mt-1 text-sm text-ink">
                    {CATEGORY_LABEL[g.category] || g.category}
                  </p>
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <StatusBadge
                      status={g.status}
                      label={STATUS_LABEL[g.status] || g.status}
                    />
                    {g.is_overdue && <StatusBadge status="overdue" />}
                  </div>
                </button>
              </li>
            ))}
          </ul>

          {/* ----------------------------------------------------- detail -- */}
          {selected && (
            <div className="space-y-4">
              <div className="card p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="font-mono text-sm text-muted">{selected.reference}</p>
                    <h2 className="mt-0.5 font-semibold text-ink">
                      {CATEGORY_LABEL[selected.category] || selected.category}
                    </h2>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <StatusBadge
                      status={selected.status}
                      label={STATUS_LABEL[selected.status] || selected.status}
                    />
                    {selected.escalated && <StatusBadge status="escalated" />}
                  </div>
                </div>

                <div className="mt-4">
                  <TimelineTracker
                    steps={GRIEVANCE_STEPS}
                    status={selected.status}
                  />
                </div>

                <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-3">
                  <div>
                    <dt className="text-xs text-muted">Filed</dt>
                    <dd className="text-ink">{fmt(selected.submitted_at)}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted">Response due</dt>
                    <dd className={selected.is_overdue ? "text-danger" : "text-ink"}>
                      {fmt(selected.deadline_at)}
                      {selected.is_overdue && " — overdue"}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted">Open for</dt>
                    <dd className="text-ink">{selected.days_open} days</dd>
                  </div>
                </dl>

                {/* React escapes this. The API returns it raw on purpose — see
                    the route's comment on why storing escaped text is worse. */}
                <div className="mt-4 rounded-lg border border-line bg-canvas p-3">
                  <p className="text-xs text-muted">What you told us</p>
                  <p className="mt-1 whitespace-pre-wrap text-sm text-ink">
                    {selected.description}
                  </p>
                </div>
              </div>

              {selected.escalated && (
                <div className="card border-warning/40 bg-warning/5 p-4 text-sm">
                  <p className="font-semibold text-ink">
                    Escalated to the Grievance Officer
                  </p>
                  <p className="mt-1 text-muted">
                    This complaint passed its escalation threshold on{" "}
                    {fmt(selected.escalated_at)}
                    {officer?.published ? ` and was sent to ${officer.name}` : ""}.
                  </p>
                </div>
              )}

              {selected.status === "resolved" && selected.resolution_notes && (
                <div className="card border-success/40 bg-success/5 p-4">
                  <p className="text-sm font-semibold text-ink">How it was resolved</p>
                  <p className="mt-1 whitespace-pre-wrap text-sm text-ink">
                    {selected.resolution_notes}
                  </p>
                  <p className="mt-2 text-xs text-muted">
                    Resolved {fmt(selected.resolved_at)}.
                  </p>
                </div>
              )}

              {selected.status === "rejected" && (
                <div className="card border-danger/40 bg-danger/5 p-4">
                  <p className="text-sm font-semibold text-ink">
                    This complaint was not upheld
                  </p>
                  <p className="mt-1 whitespace-pre-wrap text-sm text-ink">
                    {selected.rejection_reason}
                  </p>
                  <p className="mt-3 text-xs text-muted">
                    If you disagree with this outcome, you may approach the Data
                    Protection Board of India. You have now exhausted this
                    organisation&rsquo;s redressal mechanism, which is the step the
                    Act requires first.
                  </p>
                </div>
              )}

              {/* ------------------------------------------------ rating -- */}
              {selected.status === "resolved" &&
                selected.satisfaction_rating == null && (
                  <div className="card p-5">
                    <p className="text-sm font-semibold text-ink">
                      Are you satisfied with this outcome?
                    </p>
                    <p className="mt-1 text-xs text-muted">
                      One or two stars reopens the complaint — this is not just a
                      score.
                    </p>
                    <div className="mt-3 flex items-center gap-1">
                      {[1, 2, 3, 4, 5].map((n) => (
                        <button
                          key={n}
                          type="button"
                          onClick={() => setRating(n)}
                          aria-label={`${n} out of 5`}
                          aria-pressed={rating === n}
                          className={`h-9 w-9 rounded-lg border text-lg transition ${
                            n <= rating
                              ? "border-teal bg-teal/10 text-teal"
                              : "border-line text-muted hover:bg-line/40"
                          }`}
                        >
                          ★
                        </button>
                      ))}
                    </div>
                    <textarea
                      className="input mt-3 min-h-[80px]"
                      placeholder="Anything you want to add (optional)"
                      value={comment}
                      maxLength={4000}
                      onChange={(e) => setComment(e.target.value)}
                    />
                    <button
                      type="button"
                      className="btn-primary mt-3"
                      onClick={sendFeedback}
                      disabled={busy || rating === 0}
                    >
                      {rating > 0 && rating <= 2
                        ? "Submit and reopen"
                        : "Submit feedback"}
                    </button>
                  </div>
                )}

              {selected.satisfaction_rating != null && (
                <div className="card p-4 text-sm">
                  <p className="text-ink">
                    You rated this outcome {selected.satisfaction_rating}/5.
                  </p>
                  {selected.satisfaction_comment && (
                    <p className="mt-1 text-muted">
                      &ldquo;{selected.satisfaction_comment}&rdquo;
                    </p>
                  )}
                  {selected.status === "reopened" && (
                    <p className="mt-2 text-xs text-warning">
                      Because you were not satisfied, this complaint has been
                      reopened. The original deadline still applies — an
                      unsatisfactory resolution does not buy more time.
                    </p>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
