// ============================================================================
// Data Retention Policy (/admin/retention)
// Policy per data category, an edit form, a manual purge behind a confirmation,
// a scheduled-purge calendar, and exemption management for data the law makes
// us keep.
// ============================================================================
import { useEffect, useMemo, useState } from "react";
import { getAuditLogs, getRetentionPolicies, runPurge, updateRetentionPolicy } from "../../api";
import { useApp } from "../../context/AppContext";
import ConfirmModal from "../../components/common/ConfirmModal";
import StatusBadge from "../../components/common/StatusBadge";
import { previewLock } from "../../config/modules";

const DAY = 864e5;

export default function DataRetentionPolicy() {
  const { notify } = useApp();
  const [policies, setPolicies] = useState([]);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState(null);
  const [purging, setPurging] = useState(null);
  const [purgeResult, setPurgeResult] = useState(null);
  const [purgeLog, setPurgeLog] = useState([]);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    setPolicies(await getRetentionPolicies());
    const logs = await getAuditLogs();
    setPurgeLog(logs.filter((l) => l.action_type === "purge_run"));
  };

  useEffect(() => {
    load();
  }, []);

  const startEdit = (p) => {
    setEditing(p.id);
    setForm({ ...p });
  };

  const save = async () => {
    setBusy(true);
    try {
      await updateRetentionPolicy(editing, {
        retention_days: Number(form.retention_days),
        auto_delete: form.auto_delete,
        exemption: form.exemption || null,
        notify_days: Number(form.notify_days),
      });
      await load();
      setEditing(null);
      notify("Retention policy updated and logged.");
    } finally {
      setBusy(false);
    }
  };

  const purge = async () => {
    setBusy(true);
    try {
      const res = await runPurge(purging.id);
      setPurgeResult(res);
      await load();
      notify(`${res.records_deleted} records purged from ${res.category}.`);
    } finally {
      setBusy(false);
      setPurging(null);
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
        <div className="card border-success/40 bg-success/5 p-4 text-sm">
          <p className="flex items-center gap-2 font-medium text-ink">
            <span className="h-2 w-2 rounded-full bg-success" aria-hidden="true" />
            Purge completed — {purgeResult.category}
          </p>
          <p className="mt-1 text-muted">
            {purgeResult.records_deleted} records deleted on {purgeResult.at}. Logged as{" "}
            <span className="font-mono">{purgeResult.audit.log_id}</span>.
          </p>
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
                  <button type="button" className="btn-primary" onClick={save} disabled={busy} {...previewLock("retention", "Saving a retention policy")}>
                    {busy ? "Saving…" : "Save policy"}
                  </button>
                  <button type="button" className="btn-ghost" onClick={() => setEditing(null)}>
                    Cancel
                  </button>
                </div>
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
                  <button type="button" className="btn-secondary" onClick={() => startEdit(p)}>
                    Edit policy
                  </button>
                  <button type="button" className="btn-danger" onClick={() => setPurging(p)} {...previewLock("retention", "Running a purge")}>
                    Run Manual Purge
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
          <p className="text-xs text-muted">All purges are written to the audit trail.</p>
        </div>
        {purgeLog.length === 0 ? (
          <p className="px-5 py-4 text-sm text-muted">No purges recorded in this session.</p>
        ) : (
          <ul className="divide-y divide-line">
            {purgeLog.map((l) => (
              <li key={l.id} className="flex flex-wrap items-center gap-3 px-5 py-3 text-sm">
                <span className="font-mono text-xs">{l.log_id}</span>
                <span className="text-ink">{l.consent_status}</span>
                <span className="text-xs text-muted">{new Date(l.timestamp).toLocaleString()}</span>
                <span className="text-xs text-muted">by {l.initiator}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <ConfirmModal
        open={Boolean(purging)}
        title={`Run a manual purge on ${purging?.category}?`}
        body="This deletes personal data whose retention period has expired."
        consequences={[
          "Records past the retention period are permanently deleted.",
          purging?.exemption
            ? `Records covered by the exemption (“${purging.exemption}”) are kept.`
            : "There is no exemption on this category — everything expired is deleted.",
          "The result is written to the audit trail.",
          "This cannot be undone.",
        ]}
        confirmLabel="Run purge"
        busy={busy}
        onCancel={() => setPurging(null)}
        onConfirm={purge}
      />
    </div>
  );
}
