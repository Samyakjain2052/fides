// ============================================================================
// Breach register (/admin/breaches) — DPDP §8(6)
//
// Real, against /v1/breaches.
//
// The screen is organised around the one thing that goes wrong: somebody marks a
// breach handled having done half the duty. So the two obligations are shown as
// two separate, separately-tracked steps, and neither can be faked from here —
// the status follows the work, and a CHECK constraint refuses the half-state at
// the database.
//
// Three deliberate pieces of honesty:
//
//   * The Board notification is labelled as a human's action throughout. The
//     product generates the text; a person submits it and records the reference.
//   * Principal notification progress is "4,812 of 10,000 notified", counted from
//     rows. A green tick at 48% would be a lie.
//   * The 72-hour countdown is labelled as OUR reading of "without delay", not a
//     statutory figure. The Rules do not give a number.
//
// Reuses SLACountdown rather than inventing a second visual language for
// lateness. Its thresholds are day-based, so a 72-hour clock reads amber from the
// start and red inside 48 hours — which is the right emphasis for a breach.
// ============================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  attachAffected,
  boardNotice,
  changeStatus,
  closeBreach,
  getBreach,
  listAffected,
  listBreaches,
  notifyBoard,
  notifyPrincipals,
  recordBreach,
  SEVERITIES,
  STATUS_LABEL,
  updateBreach,
  voidBreach,
} from "../../api/breaches";
import { apiFetch } from "../../api/auth";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";
import SlideOver from "../../components/common/SlideOver";
import ConfirmModal from "../../components/common/ConfirmModal";
import SLACountdown from "../../components/common/SLACountdown";

const SEVERITY_TONE = {
  low: "text-muted",
  medium: "text-ink",
  high: "text-warning",
  critical: "text-danger font-semibold",
};

function fmt(iso) {
  return iso ? new Date(iso).toLocaleString() : "—";
}

export default function BreachManagement() {
  const { notify } = useApp();

  const [page, setPage] = useState(null);
  const [categories, setCategories] = useState([]);
  const [selected, setSelected] = useState(null);
  const [affected, setAffected] = useState([]);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showRecord, setShowRecord] = useState(false);
  const [confirmVoid, setConfirmVoid] = useState(false);
  const [voidReason, setVoidReason] = useState("");

  // draft form
  const [form, setForm] = useState({
    title: "",
    description: "",
    severity: "high",
    discoveredAt: new Date().toISOString().slice(0, 16),
    categories: [],
    estimatedCount: "",
  });

  // detail working fields
  const [remediation, setRemediation] = useState("");
  const [rootCause, setRootCause] = useState("");
  const [submittedBy, setSubmittedBy] = useState("");
  const [boardRef, setBoardRef] = useState("");
  const [exemption, setExemption] = useState("");

  const load = useCallback(async () => {
    try {
      setPage(await listBreaches());
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
    // The category vocabulary comes from the purposes that actually exist, so the
    // affected-query offers real categories rather than an invented list.
    apiFetch("/purposes")
      .then((rows) => setCategories([...new Set(rows.map((p) => p.category))].sort()))
      .catch(() => setCategories([]));
  }, [load]);

  useEffect(() => {
    if (!selected) return;
    setRemediation(selected.remediation || "");
    setRootCause(selected.root_cause || "");
    setExemption(selected.notification_exemption || "");
    setBoardRef(selected.board_reference || "");
    listAffected(selected.id).then(setAffected).catch(() => setAffected([]));
    boardNotice(selected.id).then(setNotice).catch(() => setNotice(null));
  }, [selected]);

  const act = async (fn, message) => {
    setBusy(true);
    setError("");
    try {
      const updated = await fn();
      if (updated?.id) setSelected(updated);
      await load();
      if (message) notify(message);
      return updated;
    } catch (e) {
      setError(e.message);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const onRecord = () =>
    act(async () => {
      const created = await recordBreach({
        ...form,
        discoveredAt: form.discoveredAt
          ? new Date(form.discoveredAt).toISOString()
          : null,
      });
      setShowRecord(false);
      setForm({
        title: "", description: "", severity: "high",
        discoveredAt: new Date().toISOString().slice(0, 16),
        categories: [], estimatedCount: "",
      });
      setSelected(created);
      return created;
    }, "Breach recorded as a draft.");

  /**
   * Run the notification to completion, one batch at a time.
   *
   * The loop lives here rather than on the server so the person watching sees the
   * count climb and can close the tab without losing anything — every batch is
   * durable, and reopening resumes where it stopped.
   */
  const onNotifyPrincipals = () =>
    act(async () => {
      let current = await notifyPrincipals(selected.id, 100);
      let guard = 0;
      while (!current.progress.complete && guard < 200) {
        setSelected(current);
        current = await notifyPrincipals(selected.id, 100);
        guard += 1;
      }
      return current;
    }, "Notification run finished.");

  const counts = page?.counts ?? {};
  const attention = useMemo(
    () => (counts.board_overdue || 0) + (counts.critical_open || 0),
    [counts],
  );

  if (!page) {
    return <p className="text-sm text-muted">{error || "Loading the register…"}</p>;
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">Breach register</h1>
          <p className="text-sm text-muted">
            DPDP §8(6). On becoming aware of a breach you must notify both the Data
            Protection Board and every affected data principal.
          </p>
        </div>
        <button type="button" className="btn-primary" onClick={() => setShowRecord(true)}>
          Record a breach
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-danger/50 bg-danger/10 p-3 text-sm text-ink">
          {error}
        </div>
      )}

      {/* --------------------------------------------------------- headline -- */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[
          { label: "Open", value: counts.open, tone: "text-ink" },
          { label: "High or critical", value: counts.critical_open, tone: "text-warning" },
          {
            label: "Board notice overdue",
            value: counts.board_overdue,
            tone: "text-danger",
            title: `Past ${page.board_threshold_hours}h since discovery with no Board notification recorded.`,
          },
          {
            label: "People still to notify",
            value: counts.awaiting_principal_notice,
            tone: "text-warning",
            title: "Open breaches where the affected data principals have not all been told. This is a separate obligation from notifying the Board.",
          },
        ].map((c) => (
          <div key={c.label} className="card p-4" title={c.title}>
            <p className="text-xs text-muted">{c.label}</p>
            <p className={`mt-1 text-2xl font-semibold ${c.tone}`}>{c.value ?? 0}</p>
          </div>
        ))}
      </div>

      <p className="text-xs text-muted">{page.board_threshold_note}</p>
      {attention > 0 && (
        <p className="text-xs text-danger">
          {attention} item{attention === 1 ? "" : "s"} need attention now.
        </p>
      )}

      {/* ------------------------------------------------------------ table -- */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-line">
            <thead className="bg-canvas">
              <tr>
                <th className="th">Reference</th>
                <th className="th">Title</th>
                <th className="th">Severity</th>
                <th className="th">Status</th>
                <th className="th">Aware</th>
                <th className="th">Board notice due</th>
                <th className="th">Affected</th>
                <th className="th sr-only">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {page.items.length === 0 && (
                <tr>
                  <td className="td text-center text-muted" colSpan={8}>
                    No breaches recorded. That is the hoped-for state, not a
                    loading error.
                  </td>
                </tr>
              )}
              {page.items.map((b) => (
                <tr key={b.id} className={b.board_overdue ? "bg-danger/5" : ""}>
                  <td className="td font-mono text-xs">{b.reference}</td>
                  <td className="td">{b.title}</td>
                  <td className={`td text-xs ${SEVERITY_TONE[b.severity]}`}>
                    {b.severity}
                  </td>
                  <td className="td">
                    <StatusBadge
                      status={b.status === "void" ? "none" : b.status}
                      label={STATUS_LABEL[b.status] || b.status}
                    />
                  </td>
                  <td className="td text-xs text-muted">
                    {b.discovered_at ? (
                      <>
                        {new Date(b.discovered_at).toLocaleDateString()}
                        <span className="ml-1">
                          ({Math.round(b.hours_since_discovery)}h ago)
                        </span>
                      </>
                    ) : (
                      // Not "0h ago". A missing awareness date is a gap, and the
                      // deadline computed from it would be fabricated.
                      <span className="text-warning">not recorded</span>
                    )}
                  </td>
                  <td className="td text-xs">
                    {b.board_notified_at ? (
                      <span className="text-success">
                        notified {new Date(b.board_notified_at).toLocaleDateString()}
                      </span>
                    ) : b.board_deadline_at ? (
                      <SLACountdown deadlineAt={b.board_deadline_at} />
                    ) : (
                      <span className="text-muted">—</span>
                    )}
                  </td>
                  <td className="td text-xs">{b.affected_count}</td>
                  <td className="td">
                    <button
                      type="button"
                      className="text-sm text-teal underline"
                      onClick={() => getBreach(b.id).then(setSelected)}
                    >
                      Open
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* ------------------------------------------------------ record form -- */}
      <SlideOver
        open={showRecord}
        onClose={() => setShowRecord(false)}
        title="Record a breach"
        subtitle="Saved as a draft — partial information now beats a complete form later."
      >
        <div className="space-y-3">
          <div>
            <label className="label" htmlFor="b-title">What happened</label>
            <input
              id="b-title"
              className="input"
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
              placeholder="Misconfigured storage bucket"
            />
          </div>
          <div>
            <label className="label" htmlFor="b-desc">Details</label>
            <textarea
              id="b-desc"
              className="input min-h-[110px]"
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="b-sev">Severity</label>
              <select
                id="b-sev"
                className="input"
                value={form.severity}
                onChange={(e) => setForm({ ...form, severity: e.target.value })}
              >
                {SEVERITIES.map((s) => (
                  <option key={s.id} value={s.id}>{s.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="label" htmlFor="b-count">Estimated people affected</label>
              <input
                id="b-count"
                className="input"
                type="number"
                min="0"
                value={form.estimatedCount}
                onChange={(e) => setForm({ ...form, estimatedCount: e.target.value })}
              />
            </div>
          </div>
          <div>
            <label className="label" htmlFor="b-aware">
              When did you become aware?
            </label>
            <input
              id="b-aware"
              className="input"
              type="datetime-local"
              value={form.discoveredAt}
              onChange={(e) => setForm({ ...form, discoveredAt: e.target.value })}
            />
            <p className="mt-1 text-xs text-muted">
              Every deadline is measured from this, not from when the breach
              happened. Changing it later requires a reason and is recorded.
            </p>
          </div>
          {categories.length > 0 && (
            <div>
              <span className="label">Categories of data affected</span>
              <div className="mt-1 flex flex-wrap gap-2">
                {categories.map((c) => {
                  const on = form.categories.includes(c);
                  return (
                    <button
                      key={c}
                      type="button"
                      onClick={() =>
                        setForm({
                          ...form,
                          categories: on
                            ? form.categories.filter((x) => x !== c)
                            : [...form.categories, c],
                        })
                      }
                      className={`rounded-full px-3 py-1 text-xs transition ${
                        on
                          ? "bg-navy text-white"
                          : "border border-line bg-surface text-ink hover:bg-line/40"
                      }`}
                    >
                      {c}
                    </button>
                  );
                })}
              </div>
              <p className="mt-1 text-xs text-muted">
                Used to find who was affected, so it has to match the purpose
                categories your consents use.
              </p>
            </div>
          )}
          <button
            type="button"
            className="btn-primary w-full"
            onClick={onRecord}
            disabled={busy || form.title.length < 3 || form.description.length < 10}
          >
            {busy ? "Recording…" : "Record breach"}
          </button>
        </div>
      </SlideOver>

      {/* ------------------------------------------------------------ detail -- */}
      <SlideOver
        open={Boolean(selected)}
        onClose={() => setSelected(null)}
        title={selected ? `${selected.reference} — ${selected.title}` : ""}
      >
        {selected && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge
                status={selected.status === "void" ? "none" : selected.status}
                label={STATUS_LABEL[selected.status] || selected.status}
              />
              <span className={`text-xs ${SEVERITY_TONE[selected.severity]}`}>
                {selected.severity}
              </span>
              {selected.board_overdue && <StatusBadge status="overdue" />}
            </div>

            {selected.status === "void" && (
              <p className="rounded-lg border border-line bg-canvas p-3 text-xs text-muted">
                Recorded in error and kept: {selected.void_reason}
              </p>
            )}

            <p className="whitespace-pre-wrap text-sm text-ink">
              {selected.description}
            </p>

            <dl className="grid grid-cols-2 gap-3 text-sm">
              <div>
                <dt className="text-xs text-muted">Became aware</dt>
                <dd className="text-ink">{fmt(selected.discovered_at)}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Occurred</dt>
                <dd className="text-ink">{fmt(selected.occurred_at)}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Contained</dt>
                <dd className="text-ink">{fmt(selected.contained_at)}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Categories</dt>
                <dd className="text-ink">
                  {selected.categories_affected.join(", ") || "—"}
                </dd>
              </div>
            </dl>

            {selected.is_open !== false && selected.status !== "void" && (
              <>
                {/* ------------------------------------- the two obligations -- */}
                <div className="space-y-3 border-t border-line pt-4">
                  <p className="text-sm font-semibold text-ink">
                    The notification duty
                  </p>
                  <p className="text-xs text-muted">
                    Two separate obligations. A breach is only &ldquo;notified&rdquo;
                    when both are done — the status follows the work, and the
                    database refuses the half-state.
                  </p>

                  {/* --- 1. the Board --- */}
                  <div className="rounded-lg border border-line p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <p className="text-sm font-medium text-ink">
                        1. The Data Protection Board
                      </p>
                      {selected.board_notified_at ? (
                        <StatusBadge status="completed" label="Recorded" />
                      ) : (
                        <StatusBadge status="pending" label="Outstanding" />
                      )}
                    </div>

                    {selected.board_notified_at ? (
                      <p className="mt-2 text-xs text-muted">
                        Submitted by {selected.board_submitted_by} on{" "}
                        {fmt(selected.board_notified_at)}
                        {selected.board_reference &&
                          `, reference ${selected.board_reference}`}
                        .
                      </p>
                    ) : (
                      <div className="mt-2 space-y-2">
                        <p className="text-xs text-muted">
                          {notice?.note ||
                            "This product does not transmit anything to the Board."}
                        </p>
                        {notice?.content && (
                          <details>
                            <summary className="cursor-pointer text-xs text-teal underline">
                              Show the text to submit
                            </summary>
                            <pre className="mt-2 max-h-64 overflow-auto rounded-lg border border-line bg-canvas p-3 text-[11px] leading-relaxed text-ink">
                              {notice.content}
                            </pre>
                          </details>
                        )}
                        <div className="grid gap-2 sm:grid-cols-2">
                          <input
                            className="input"
                            placeholder="Who submitted it"
                            value={submittedBy}
                            onChange={(e) => setSubmittedBy(e.target.value)}
                          />
                          <input
                            className="input"
                            placeholder="Reference from the Board"
                            value={boardRef}
                            onChange={(e) => setBoardRef(e.target.value)}
                          />
                        </div>
                        <button
                          type="button"
                          className="btn-secondary w-full"
                          disabled={busy || submittedBy.trim().length < 2}
                          onClick={() =>
                            act(
                              () =>
                                notifyBoard(selected.id, {
                                  submittedBy,
                                  boardReference: boardRef,
                                }),
                              "Board submission recorded.",
                            )
                          }
                        >
                          Record that this was submitted
                        </button>
                      </div>
                    )}
                  </div>

                  {/* --- 2. the people --- */}
                  <div className="rounded-lg border border-line p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <p className="text-sm font-medium text-ink">
                        2. Every affected data principal
                      </p>
                      {selected.progress.complete ? (
                        <StatusBadge status="completed" label="Done" />
                      ) : (
                        <StatusBadge status="pending" label="Outstanding" />
                      )}
                    </div>

                    {/* The truth, not a tick. */}
                    <p className="mt-2 text-sm text-ink">
                      {selected.progress.summary}
                      {selected.progress.suppressed > 0 &&
                        ` · ${selected.progress.suppressed} unreachable`}
                    </p>
                    {selected.progress.total > 0 && (
                      <div
                        className="mt-2 h-2 overflow-hidden rounded-full bg-line"
                        role="progressbar"
                        aria-valuenow={selected.progress.notified}
                        aria-valuemax={selected.progress.total}
                      >
                        <div
                          className="h-full bg-teal"
                          style={{
                            width: `${
                              (100 * selected.progress.notified) /
                              selected.progress.total
                            }%`,
                          }}
                        />
                      </div>
                    )}

                    {selected.affected_count === 0 ? (
                      <div className="mt-3 space-y-2">
                        <p className="text-xs text-muted">
                          Nobody is on the affected list. Attach them by category,
                          and review who that is before anything is sent.
                        </p>
                        <div className="flex flex-wrap gap-2">
                          <button
                            type="button"
                            className="btn-secondary"
                            disabled={
                              busy || selected.categories_affected.length === 0
                            }
                            onClick={() =>
                              act(
                                () =>
                                  attachAffected(selected.id, {
                                    categories: selected.categories_affected,
                                  }),
                                "Affected list attached — review it before notifying.",
                              )
                            }
                          >
                            Attach everyone in{" "}
                            {selected.categories_affected.join(", ") ||
                              "(no categories set)"}
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div className="mt-3 space-y-2">
                        <details>
                          <summary className="cursor-pointer text-xs text-teal underline">
                            Review the {selected.affected_count} people on the list
                          </summary>
                          <ul className="mt-2 max-h-56 space-y-1 overflow-auto text-xs">
                            {affected.map((a) => (
                              <li
                                key={a.principal_id}
                                className="flex flex-wrap items-center gap-2 rounded border border-line px-2 py-1"
                              >
                                <span className="text-ink">
                                  {a.email || a.principal_ref || "(no address)"}
                                </span>
                                <span className="tag">{a.source}</span>
                                {a.notified_at ? (
                                  a.suppressed_reason ? (
                                    <span className="ml-auto text-warning">
                                      {a.suppressed_reason}
                                    </span>
                                  ) : (
                                    <span className="ml-auto text-success">
                                      notified
                                    </span>
                                  )
                                ) : (
                                  <span className="ml-auto text-muted">pending</span>
                                )}
                              </li>
                            ))}
                          </ul>
                        </details>

                        {!remediation && (
                          <p className="text-xs text-warning">
                            Record what you have done about it first — a notice that
                            describes a breach and offers no remedy tells somebody
                            they have a problem and nothing they can do.
                          </p>
                        )}
                        {!selected.progress.complete && (
                          <button
                            type="button"
                            className="btn-secondary w-full"
                            disabled={busy || !selected.remediation}
                            onClick={onNotifyPrincipals}
                          >
                            {busy
                              ? "Sending…"
                              : selected.progress.notified > 0
                                ? `Resume — ${selected.progress.remaining} remaining`
                                : `Notify ${selected.affected_count} people`}
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                </div>

                {/* ------------------------------------------- cause and fix -- */}
                <div className="space-y-3 border-t border-line pt-4">
                  <div>
                    <label className="label" htmlFor="b-remed">
                      What you have done about it
                    </label>
                    <textarea
                      id="b-remed"
                      className="input min-h-[80px]"
                      value={remediation}
                      onChange={(e) => setRemediation(e.target.value)}
                    />
                  </div>
                  <div>
                    <label className="label" htmlFor="b-cause">Root cause</label>
                    <textarea
                      id="b-cause"
                      className="input min-h-[80px]"
                      value={rootCause}
                      onChange={(e) => setRootCause(e.target.value)}
                    />
                  </div>
                  <button
                    type="button"
                    className="btn-ghost"
                    disabled={busy}
                    onClick={() =>
                      act(
                        () =>
                          updateBreach(selected.id, { remediation, rootCause }),
                        "Saved.",
                      )
                    }
                  >
                    Save notes
                  </button>

                  {!selected.progress.complete && selected.affected_count > 0 && (
                    <div>
                      <label className="label" htmlFor="b-exempt">
                        Exemption, if these people will not be told
                      </label>
                      <textarea
                        id="b-exempt"
                        className="input min-h-[70px]"
                        value={exemption}
                        onChange={(e) => setExemption(e.target.value)}
                        placeholder="Why the remaining people will not be notified."
                      />
                      <p className="mt-1 text-xs text-muted">
                        Required to close with people un-notified. This is a
                        decision somebody may be asked to justify.
                      </p>
                    </div>
                  )}

                  <div className="flex flex-wrap gap-2">
                    {selected.status === "draft" && (
                      <button
                        type="button"
                        className="btn-secondary"
                        disabled={busy}
                        onClick={() =>
                          act(
                            () => changeStatus(selected.id, "investigating"),
                            "Moved to investigating.",
                          )
                        }
                      >
                        Start investigating
                      </button>
                    )}
                    {["investigating"].includes(selected.status) && (
                      <button
                        type="button"
                        className="btn-secondary"
                        disabled={busy}
                        onClick={() =>
                          act(
                            () => changeStatus(selected.id, "contained"),
                            "Marked contained.",
                          )
                        }
                      >
                        Mark contained
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn-primary"
                      disabled={busy || !rootCause.trim() || !remediation.trim()}
                      onClick={() =>
                        act(
                          () =>
                            closeBreach(selected.id, {
                              rootCause,
                              remediation,
                              exemption,
                            }),
                          "Closed.",
                        )
                      }
                    >
                      Close
                    </button>
                    <button
                      type="button"
                      className="btn-ghost text-danger"
                      disabled={busy}
                      onClick={() => setConfirmVoid(true)}
                    >
                      Recorded in error
                    </button>
                  </div>
                </div>
              </>
            )}

            {/* -------------------------------------------------- timeline -- */}
            {selected.timeline?.length > 0 && (
              <div className="border-t border-line pt-4">
                <p className="text-sm font-semibold text-ink">Timeline</p>
                <ul className="mt-2 space-y-2">
                  {selected.timeline.map((e, i) => (
                    <li key={i} className="rounded-lg border border-line px-3 py-2 text-xs">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-ink">
                          {e.from_status && e.from_status !== e.to_status
                            ? `${STATUS_LABEL[e.from_status] || e.from_status} → ${
                                STATUS_LABEL[e.to_status] || e.to_status
                              }`
                            : STATUS_LABEL[e.to_status] || e.to_status}
                        </span>
                        {e.automated && <span className="tag">automatic</span>}
                        <span className="ml-auto text-muted">{fmt(e.created_at)}</span>
                      </div>
                      {e.note && <p className="mt-1 text-muted">{e.note}</p>}
                      {e.actor_label && (
                        <p className="mt-0.5 text-muted">by {e.actor_label}</p>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </SlideOver>

      <ConfirmModal
        open={confirmVoid}
        title="Mark this entry as recorded in error?"
        body="The entry is kept, with your reason attached. Nothing in this register is ever deleted."
        consequences={[
          "It can no longer be worked on or notified.",
          "A register whose entries can vanish is not a register — so this is the closest thing to a delete.",
        ]}
        confirmLabel="Mark as an error"
        extra={
          <textarea
            className="input min-h-[70px]"
            placeholder="Why was this recorded in error?"
            value={voidReason}
            onChange={(e) => setVoidReason(e.target.value)}
          />
        }
        onCancel={() => {
          setConfirmVoid(false);
          setVoidReason("");
        }}
        onConfirm={() =>
          act(() => voidBreach(selected.id, voidReason), "Marked as recorded in error.")
            .then(() => {
              setConfirmVoid(false);
              setVoidReason("");
            })
        }
      />
    </div>
  );
}
