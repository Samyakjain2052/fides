// ============================================================================
// Data Retention Policy (/admin/retention)
// Policy per data category, an edit form, a manual purge behind a confirmation,
// a scheduled-purge calendar, and exemption management for data the law makes
// us keep.
// ============================================================================
import { useEffect, useMemo, useState } from "react";
import {
  listPolicies,
  listRuns,
  preview as previewPurge,
  runItems,
  runPurge,
  updatePolicy,
} from "../../api/retention";
import { useApp } from "../../context/AppContext";
import ConfirmModal from "../../components/common/ConfirmModal";
import StatusBadge from "../../components/common/StatusBadge";

const DAY = 864e5;

export default function DataRetentionPolicy() {
  const { notify } = useApp();
  const [policies, setPolicies] = useState([]);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState(null);
  // Set when the server refuses to shorten an auto-delete window unconfirmed.
  const [confirmShorten, setConfirmShorten] = useState(null);
  const [purging, setPurging] = useState(null);
  const [purgeResult, setPurgeResult] = useState(null);
  const [purgeLog, setPurgeLog] = useState([]);
  const [busy, setBusy] = useState(false);
  // Typed by a human, never pre-filled. That is the whole point of it.
  const [confirmText, setConfirmText] = useState("");

  const load = async () => {
    setPolicies(await listPolicies());
    // Real run history, not audit entries filtered by a guessed action name.
    setPurgeLog(await listRuns());
  };

  useEffect(() => {
    load();
  }, []);

  const startEdit = (p) => {
    setEditing(p.id);
    setForm({ ...p });
  };

  /**
   * Save an edit.
   *
   * Shortening the window on an auto-delete policy is refused by the server
   * unless confirmed, because it enlarges an unattended destruction set with no
   * button pressed. Rather than sending `confirm_shortening` optimistically, the
   * refusal is surfaced and the confirmation asked for — the point of the guard is
   * that somebody sees it.
   */
  const save = async (confirmShortening = false) => {
    setBusy(true);
    try {
      await updatePolicy(editing, {
        name: form.name,
        retentionDays: form.retention_days,
        notifyDays: form.notify_days,
        autoDelete: form.auto_delete,
        exemptionCode: form.exemption_code,
        exemptionReference: form.exemption_reference,
        confirmShortening,
      });
      await load();
      setEditing(null);
      setConfirmShorten(null);
      notify("Policy updated. The change is in the audit trail.");
    } catch (e) {
      // A refusal to shorten is not an error to shrug at — ask for the
      // confirmation the server is holding out for.
      if (/without anybody pressing anything/.test(e.message)) {
        setConfirmShorten(e.message);
      } else {
        notify(e.message, "error");
      }
    } finally {
      setBusy(false);
    }
  };

  /** The PRIMARY action. Reports what would happen, changes nothing. */
  const preview = async (policy) => {
    setBusy(true);
    try {
      const run = await previewPurge(policy.id);
      const items = await runItems(run.id);
      setPurgeResult({ ...run, items, policy });
      await load();
      notify(
        `${run.candidates_found} record(s) would be ${policy.action}ed. Nothing was changed.`
      );
    } catch (e) {
      notify(e.message || "The preview could not be run.", "error");
    } finally {
      setBusy(false);
    }
  };

  /**
   * The live run. Irreversible.
   *
   * The confirmation is the policy's own name, typed by a human — the server
   * refuses anything else, and filling it in here would defeat the point.
   */
  const purge = async () => {
    setBusy(true);
    try {
      const run = await runPurge(purging.id, confirmText);
      const items = await runItems(run.id);
      setPurgeResult({ ...run, items, policy: purging });
      await load();
      notify(`${run.rows_affected} record(s) purged. The receipt is on this page.`);
    } catch (e) {
      // The server refuses for real reasons — a wrong confirmation, an exempt
      // policy, an inactive one. Each is fixable, and each says how.
      notify(e.message || "The purge could not be run.", "error");
    } finally {
      setBusy(false);
      setPurging(null);
      setConfirmText("");
    }
  };

  // Next purge = last purge + retention period, shown as a simple calendar list.
  const schedule = useMemo(
    () =>
      policies
        .map((p) => {
          const next = new Date(new Date(p.last_purge).getTime() + p.retention_days * DAY);
          return { ...p, next };
        })
        .sort((a, b) => a.next - b.next),
    [policies]
  );

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">Data retention policy</h1>
        <p className="text-sm text-muted">
          How long each category of personal data is kept, and what happens when that period ends.
        </p>
      </div>

      {purgeResult && (
        <div
          className={`card p-4 text-sm ${
            purgeResult.mode === "dry_run"
              ? "border-info/40 bg-info/5"
              : "border-success/40 bg-success/5"
          }`}
        >
          <p className="flex items-center gap-2 font-medium text-ink">
            <span
              className={`h-2 w-2 rounded-full ${
                purgeResult.mode === "dry_run" ? "bg-info" : "bg-success"
              }`}
              aria-hidden="true"
            />
            {purgeResult.mode === "dry_run"
              ? `Preview — nothing was changed (${purgeResult.policy?.name})`
              : `Purge completed — ${purgeResult.policy?.name}`}
          </p>
          <p className="mt-1 text-muted">
            {purgeResult.mode === "dry_run"
              ? `${purgeResult.candidates_found} record(s) would be affected. ` +
                `${purgeResult.scope_summary?.examined ?? 0} examined.`
              : `${purgeResult.rows_affected} record(s) ${
                  purgeResult.policy?.action === "delete" ? "deleted" : "masked"
                }.`}{" "}
            Receipt <span className="font-mono">{purgeResult.id?.slice(0, 8)}</span>.
          </p>

          {/* Every skip, with its reason. "Not purged because they have an open
              rights request" is the answer to a question somebody will ask. */}
          {purgeResult.items?.length > 0 && (
            <ul className="mt-3 space-y-1">
              {purgeResult.items.slice(0, 12).map((i) => (
                <li key={i.entity_id} className="font-mono text-xs text-muted">
                  {i.action_taken}
                  {i.skip_reason ? ` — ${i.skip_reason}` : ""}
                </li>
              ))}
            </ul>
          )}
          {purgeResult.scope_summary?.batch_capped && (
            <p className="mt-2 text-xs text-warning">
              This run was capped at {purgeResult.scope_summary.batch_cap} records.
              More remain eligible — run it again.
            </p>
          )}
        </div>
      )}

      {/* ------------------------------------------------------ policies -- */}
      <div className="space-y-4">
        {policies.map((p) => (
          <div key={p.id} className="card p-5">
            {editing === p.id ? (
              <div className="space-y-4">
                <div className="flex items-center justify-between gap-3">
                  <h2 className="font-semibold text-ink">{p.category}</h2>
                  <span className="tag">editing</span>
                </div>
                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  <div>
                    <label className="label" htmlFor={`r-days-${p.id}`}>Retention period (days)</label>
                    <input id={`r-days-${p.id}`} type="number" min="1" className="input"
                           value={form.retention_days}
                           onChange={(e) => setForm({ ...form, retention_days: e.target.value })} />
                  </div>
                  <div>
                    <label className="label" htmlFor={`r-notify-${p.id}`}>Notify admin (days before)</label>
                    <input id={`r-notify-${p.id}`} type="number" min="0" className="input"
                           value={form.notify_days}
                           onChange={(e) => setForm({ ...form, notify_days: e.target.value })} />
                  </div>
                  <div className="sm:col-span-2">
                    <label className="label" htmlFor={`r-exempt-${p.id}`}>
                      Exemption reason (required to retain beyond schedule)
                    </label>
                    <input id={`r-exempt-${p.id}`} className="input" value={form.exemption || ""}
                           placeholder="e.g. Retain if RBI mandates"
                           onChange={(e) => setForm({ ...form, exemption: e.target.value })} />
                  </div>
                </div>
                <label className="flex items-center gap-3">
                  <button type="button" role="switch" aria-checked={form.auto_delete}
                          aria-label="Auto delete"
                          onClick={() => setForm({ ...form, auto_delete: !form.auto_delete })}
                          className={`relative inline-flex h-6 w-11 items-center rounded-full transition ${
                            form.auto_delete ? "bg-teal" : "bg-line"
                          }`}>
                    <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition ${
                      form.auto_delete ? "translate-x-6" : "translate-x-1"
                    }`} />
                  </button>
                  <span className="text-sm text-ink">
                    Auto-delete when the retention period ends
                  </span>
                </label>
                <div className="flex gap-3">
                  <button type="button" className="btn-primary"
                          onClick={() => save(false)} disabled={busy}>
                    {busy ? "Saving…" : "Save policy"}
                  </button>
                  <button type="button" className="btn-ghost"
                          onClick={() => { setEditing(null); setConfirmShorten(null); }}>
                    Cancel
                  </button>
                </div>
                {confirmShorten && (
                  <div className="rounded-lg border border-danger/50 bg-danger/10 p-3">
                    <p className="text-sm font-semibold text-ink">
                      This shortens an automatic deletion window
                    </p>
                    <p className="mt-1 text-xs text-muted">{confirmShorten}</p>
                    <div className="mt-2 flex flex-wrap gap-2">
                      <button type="button" className="btn-secondary"
                              onClick={() => preview(p)} disabled={busy}>
                        Preview what would be purged
                      </button>
                      <button type="button" className="btn-danger"
                              onClick={() => save(true)} disabled={busy}>
                        I understand — save it
                      </button>
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="font-semibold text-ink">{p.category}</h2>
                    <StatusBadge status={p.auto_delete ? "active" : "pending"}
                                 label={p.auto_delete ? "Auto-delete on" : "Manual only"} />
                    {p.exemption && <span className="tag">exemption set</span>}
                  </div>
                  <dl className="mt-3 grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2 lg:grid-cols-4">
                    <div>
                      <dt className="text-xs text-muted">Retention period</dt>
                      <dd className="text-ink">
                        {p.retention_days} days ({Math.round(p.retention_days / 365)} yr)
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">Last purge</dt>
                      <dd className="text-ink">{p.last_purge}</dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">Notify before</dt>
                      <dd className="text-ink">{p.notify_days} days</dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">Exemption</dt>
                      <dd className="text-ink">{p.exemption || "none"}</dd>
                    </div>
                  </dl>
                </div>
                <div className="flex flex-wrap gap-2">
                  {/* Preview FIRST, and styled as the primary action. The
                      destructive one must never be the easiest thing to reach on
                      a screen that deletes people's data. */}
                  <button type="button" className="btn-primary" onClick={() => preview(p)}
                          disabled={busy}>
                    {busy ? "Checking…" : "Preview — what would be purged"}
                  </button>
                  <button type="button" className="btn-secondary"
                          onClick={() => startEdit(p)}>
                    Edit policy
                  </button>
                  <button type="button" className="btn-danger" onClick={() => setPurging(p)}>
                    Run purge…
                  </button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      {/* ------------------------------------------------------ schedule -- */}
      <section className="card overflow-hidden">
        <div className="border-b border-line px-5 py-4">
          <h2 className="font-semibold text-ink">Scheduled purges</h2>
          <p className="text-xs text-muted">
            Next scheduled deletion per category, earliest first.
          </p>
        </div>
        <ul className="divide-y divide-line">
          {schedule.map((p) => {
            const days = Math.ceil((p.next - Date.now()) / DAY);
            return (
              <li key={p.id} className="flex flex-wrap items-center gap-3 px-5 py-3 text-sm">
                <span className="w-40 font-medium text-ink">{p.category}</span>
                <span className="text-muted">{p.next.toLocaleDateString()}</span>
                <span className={days < 30 ? "font-medium text-warning" : "text-muted"}>
                  {days > 0 ? `in ${days} days` : "due now"}
                </span>
                {!p.auto_delete && <span className="tag">needs a manual run</span>}
                {p.exemption && (
                  <span className="ml-auto text-xs text-muted">
                    exempt: {p.exemption}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      </section>

      {/* ----------------------------------------------------- purge log -- */}
      <section className="card overflow-hidden">
        <div className="border-b border-line px-5 py-4">
          <h2 className="font-semibold text-ink">Purge activity</h2>
          <p className="text-xs text-muted">
              Every run writes a receipt that cannot afterwards be edited or deleted.
            </p>
        </div>
        {purgeLog.length === 0 ? (
          <p className="px-5 py-4 text-sm text-muted">No purges recorded in this session.</p>
        ) : (
          <ul className="divide-y divide-line">
            {purgeLog.map((l) => (
              <li key={l.id} className="flex flex-wrap items-center gap-3 px-5 py-3 text-sm">
                <span className="font-mono text-xs">{l.id.slice(0, 8)}</span>
                <span
                  className={`tag ${l.mode === "dry_run" ? "" : "text-danger"}`}
                >
                  {l.mode === "dry_run" ? "preview" : "LIVE"}
                </span>
                <span className="text-ink">
                  {l.mode === "dry_run"
                    ? `${l.candidates_found} would be affected`
                    : `${l.rows_affected} affected`}
                </span>
                <span className="text-xs text-muted">
                  {new Date(l.started_at).toLocaleString()}
                </span>
                <span className="text-xs text-muted">{l.status}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <ConfirmModal
        open={Boolean(purging)}
        title={`Run a live purge with “${purging?.name}”?`}
        body={
          `Identifiers for people past the ${purging?.retention_days}-day retention ` +
          `period in “${purging?.data_category}” will be ` +
          `${purging?.action === "delete" ? "deleted" : "masked"}. ` +
          `Run a preview first if you have not already.`
        }
        consequences={[
          "Identifiers are cleared. The consent records stay — they are the evidence the data could lawfully be held.",
          "Anyone under a legal hold, with an open rights request, or with an active consent is skipped, and the receipt says why.",
          purging?.exemption_code && purging.exemption_code !== "none"
            ? `This policy carries a ${purging.exemption_code} exemption, so the server will refuse the run.`
            : "This policy has no exemption.",
          "A receipt is written that cannot afterwards be edited or deleted.",
          "This cannot be undone.",
        ]}
        confirmLabel="Run purge"
        busy={busy || confirmText !== purging?.name}
        onCancel={() => { setPurging(null); setConfirmText(""); }}
        onConfirm={purge}
        extra={
          <label className="block text-sm">
            <span className="text-ink">
              Type the policy name to confirm: <strong>{purging?.name}</strong>
            </span>
            <input
              className="input mt-1"
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder={purging?.name}
              aria-label="Policy name confirmation"
            />
            <span className="mt-1 block text-xs text-muted">
              The server refuses anything else. This field is never pre-filled —
              it is the step that stops a mis-click destroying data.
            </span>
          </label>
        }
      />
    </div>
  );
}
