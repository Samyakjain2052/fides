// ============================================================================
// Assessments (/admin/assessments)
//
// DPIAs, RoPAs, vendor reviews and discovery surveys. §10 makes a DPIA a duty
// of every Significant Data Fiduciary; a RoPA is the document a regulator asks
// for first.
//
// The list shows two things a queue of documents usually hides: which
// assessments are overdue, and which approved ones have come round for review
// again. An approved DPIA is a claim about a system at a point in time, and a
// two-year-old one sitting in a list looking current is the failure this module
// exists to prevent.
// ============================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  KIND_LABEL,
  STATUS_LABEL,
  STATUS_TONE,
  assessments as loadAssessments,
  createAssessment,
  seedTemplates,
  templates as loadTemplates,
} from "../../api/assessments";
import { listUsers } from "../../api/auth";
import { useApp } from "../../context/AppContext";

const TONE_DOT = {
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
  info: "bg-info",
  neutral: "bg-muted",
};

function Badge({ tone, children }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-line bg-surface px-2 py-0.5 text-xs text-ink">
      {/* A dot AND a label, never colour alone. */}
      <span
        className={`h-2 w-2 shrink-0 rounded-full ${TONE_DOT[tone] || "bg-muted"}`}
        aria-hidden="true"
      />
      {children}
    </span>
  );
}

export default function Assessments() {
  const { notify } = useApp();

  const [rows, setRows] = useState([]);
  const [tpls, setTpls] = useState([]);
  const [people, setPeople] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState("open");

  const [form, setForm] = useState({
    templateId: "",
    name: "",
    managerUserId: "",
    subjectLabel: "",
    dueAt: "",
    reviewEveryDays: "",
  });

  const load = useCallback(async () => {
    try {
      const [a, t] = await Promise.all([
        loadAssessments(),
        loadTemplates({ publishedOnly: true }),
      ]);
      setRows(a);
      setTpls(t);
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    listUsers()
      .then((u) => setPeople(u.filter((p) => p.is_active)))
      .catch(() => setPeople([]));
  }, []);

  const shown = useMemo(() => {
    if (filter === "all") return rows;
    if (filter === "review_due") return rows.filter((r) => r.review_due);
    if (filter === "overdue") return rows.filter((r) => r.overdue);
    if (filter === "open")
      return rows.filter((r) =>
        ["draft", "in_progress", "in_review", "rejected"].includes(r.status),
      );
    return rows.filter((r) => r.status === filter);
  }, [rows, filter]);

  const counts = useMemo(
    () => ({
      overdue: rows.filter((r) => r.overdue).length,
      reviewDue: rows.filter((r) => r.review_due).length,
      awaiting: rows.filter((r) => r.status === "in_review").length,
    }),
    [rows],
  );

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const created = await createAssessment({
        templateId: form.templateId,
        name: form.name.trim(),
        managerUserId: form.managerUserId || null,
        subjectLabel: form.subjectLabel.trim() || null,
        dueAt: form.dueAt ? new Date(form.dueAt).toISOString() : null,
        reviewEveryDays: form.reviewEveryDays
          ? Number(form.reviewEveryDays)
          : null,
      });
      notify(`${created.name} created.`);
      setCreating(false);
      setForm({
        templateId: "",
        name: "",
        managerUserId: "",
        subjectLabel: "",
        dueAt: "",
        reviewEveryDays: "",
      });
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">Assessments</h1>
          <p className="text-sm text-muted">
            Impact assessments, processing records and vendor reviews. A DPIA is
            a duty of a Significant Data Fiduciary under §10.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {tpls.length === 0 && (
            <button
              type="button"
              className="btn-secondary"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  const r = await seedTemplates();
                  notify(`${r.count} questionnaire(s) installed.`);
                  await load();
                } catch (e) {
                  setError(e.message);
                } finally {
                  setBusy(false);
                }
              }}
            >
              Install the standard questionnaires
            </button>
          )}
          <Link to="/admin/assessments/templates" className="btn-secondary">
            Questionnaires
          </Link>
          <button
            type="button"
            className="btn-primary"
            disabled={tpls.length === 0}
            onClick={() => setCreating((v) => !v)}
            title={
              tpls.length === 0
                ? "Install or publish a questionnaire first"
                : undefined
            }
          >
            {creating ? "Cancel" : "Start an assessment"}
          </button>
        </div>
      </header>

      {/* ---------------------------------------------------- what needs doing -- */}
      {(counts.overdue > 0 || counts.reviewDue > 0 || counts.awaiting > 0) && (
        <div className="flex flex-wrap gap-3">
          {counts.overdue > 0 && (
            <button
              type="button"
              onClick={() => setFilter("overdue")}
              className="rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-left text-sm"
            >
              <strong className="font-semibold text-ink">
                {counts.overdue}
              </strong>{" "}
              past their due date
            </button>
          )}
          {counts.reviewDue > 0 && (
            <button
              type="button"
              onClick={() => setFilter("review_due")}
              className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-left text-sm"
            >
              <strong className="font-semibold text-ink">
                {counts.reviewDue}
              </strong>{" "}
              due for re-review
              <span className="block text-xs text-muted">
                Approved, but the review date has passed.
              </span>
            </button>
          )}
          {counts.awaiting > 0 && (
            <button
              type="button"
              onClick={() => setFilter("in_review")}
              className="rounded-lg border border-line bg-surface px-3 py-2 text-left text-sm"
            >
              <strong className="font-semibold text-ink">
                {counts.awaiting}
              </strong>{" "}
              awaiting sign-off
            </button>
          )}
        </div>
      )}

      {error && (
        <p className="rounded-lg border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">
          {error}
        </p>
      )}

      {/* -------------------------------------------------------------- create -- */}
      {creating && (
        <form onSubmit={submit} className="card space-y-4 p-5">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="a-template">
                Which questionnaire
              </label>
              <select
                id="a-template"
                className="input"
                value={form.templateId}
                onChange={(e) => {
                  const t = tpls.find((x) => x.id === e.target.value);
                  setForm({
                    ...form,
                    templateId: e.target.value,
                    name: form.name || (t ? t.name : ""),
                  });
                }}
                required
              >
                <option value="">Choose…</option>
                {tpls.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name} (v{t.version})
                  </option>
                ))}
              </select>
              <p className="mt-1 text-xs text-muted">
                The version is pinned, so later edits to the questionnaire
                cannot change what this assessment asked.
              </p>
            </div>
            <div>
              <label className="label" htmlFor="a-name">
                Name it
              </label>
              <input
                id="a-name"
                className="input"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="DPIA — new loyalty programme"
                required
              />
            </div>
            <div>
              <label className="label" htmlFor="a-subject">
                What is it about? (optional)
              </label>
              <input
                id="a-subject"
                className="input"
                value={form.subjectLabel}
                onChange={(e) =>
                  setForm({ ...form, subjectLabel: e.target.value })
                }
                placeholder="A system, a vendor, a project"
              />
            </div>
            <div>
              <label className="label" htmlFor="a-manager">
                Who is accountable
              </label>
              <select
                id="a-manager"
                className="input"
                value={form.managerUserId}
                onChange={(e) =>
                  setForm({ ...form, managerUserId: e.target.value })
                }
              >
                <option value="">Me</option>
                {people.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.full_name || p.email}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="label" htmlFor="a-due">
                Due by (optional)
              </label>
              <input
                id="a-due"
                type="date"
                className="input"
                value={form.dueAt}
                onChange={(e) => setForm({ ...form, dueAt: e.target.value })}
              />
            </div>
            <div>
              <label className="label" htmlFor="a-cadence">
                Re-review every (days)
              </label>
              <input
                id="a-cadence"
                type="number"
                min="1"
                className="input"
                value={form.reviewEveryDays}
                onChange={(e) =>
                  setForm({ ...form, reviewEveryDays: e.target.value })
                }
                placeholder="365"
              />
              <p className="mt-1 text-xs text-muted">
                Systems change. Without a cadence an approved assessment sits
                there looking current indefinitely.
              </p>
            </div>
          </div>
          <button
            type="submit"
            className="btn-primary"
            disabled={busy || !form.templateId || !form.name.trim()}
          >
            {busy ? "Creating…" : "Create"}
          </button>
        </form>
      )}

      {/* --------------------------------------------------------------- list -- */}
      <div className="flex flex-wrap gap-2">
        {[
          ["open", "Open"],
          ["in_review", "Awaiting sign-off"],
          ["approved", "Approved"],
          ["review_due", "Due for re-review"],
          ["overdue", "Overdue"],
          ["all", "Everything"],
        ].map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => setFilter(id)}
            className={`rounded-full border px-3 py-1 text-sm ${
              filter === id
                ? "border-navy bg-navy text-white"
                : "border-line bg-surface text-muted hover:border-navy/40"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {shown.length === 0 ? (
        <div className="card p-6 text-center">
          <p className="text-sm text-muted">
            {rows.length === 0
              ? "No assessments yet. A DPIA before a new processing activity starts is the point at which one is cheap."
              : "Nothing matches that filter."}
          </p>
        </div>
      ) : (
        <ul className="space-y-3">
          {shown.map((a) => (
            <li key={a.id} className="card p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <Link
                    to={`/admin/assessments/${a.id}`}
                    className="font-medium text-ink hover:underline"
                  >
                    {a.name}
                  </Link>
                  <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                    <Badge tone={STATUS_TONE[a.status]}>
                      {STATUS_LABEL[a.status] || a.status}
                    </Badge>
                    <span>{KIND_LABEL[a.kind] || a.kind}</span>
                    {a.subject_label && <span>· {a.subject_label}</span>}
                    {a.manager_label && <span>· {a.manager_label}</span>}
                  </p>
                  {a.conclusion && (
                    <p className="mt-2 text-sm text-ink">
                      <span className="text-xs uppercase tracking-wide text-muted">
                        Conclusion
                      </span>
                      <br />
                      {a.conclusion}
                    </p>
                  )}
                  {a.rejection_reason && (
                    <p className="mt-2 text-sm text-danger">
                      Sent back: {a.rejection_reason}
                    </p>
                  )}
                </div>
                <div className="shrink-0 text-right text-xs">
                  {a.due_at && (
                    <p className={a.overdue ? "text-danger" : "text-muted"}>
                      due {new Date(a.due_at).toLocaleDateString()}
                      {a.overdue && " · overdue"}
                    </p>
                  )}
                  {a.approved_at && (
                    <p className="text-muted">
                      approved {new Date(a.approved_at).toLocaleDateString()}
                    </p>
                  )}
                  {a.review_due && (
                    <p className="text-warning">re-review due</p>
                  )}
                  {a.next_review_at && !a.review_due && (
                    <p className="text-muted">
                      next review{" "}
                      {new Date(a.next_review_at).toLocaleDateString()}
                    </p>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
