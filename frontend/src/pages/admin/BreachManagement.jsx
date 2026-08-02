// ============================================================================
// Breach Management (/admin/breaches)
//
// NOTE ON SCOPE: the brief lists BreachManagement.jsx in the file structure but
// gives no screen specification for it, so this screen is built to the duty the
// DPDP Act actually imposes — on becoming aware of a personal data breach, a
// Data Fiduciary must notify the Data Protection Board and each affected Data
// Principal. Everything here follows the brief's own conventions (status badge
// with dot + label, ConfirmModal on the irreversible step, audit entry on every
// change). Adjust once the screen is specified.
// ============================================================================
import { useEffect, useMemo, useState } from "react";
import { getBreaches, saveBreach } from "../../api";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";
import SlideOver from "../../components/common/SlideOver";
import ConfirmModal from "../../components/common/ConfirmModal";
import StatCard from "../../components/common/StatCard";
import { previewLock } from "../../config/modules";

const SEVERITIES = ["low", "medium", "high", "critical"];
const STATUSES = ["investigating", "contained", "reported_to_dpb", "closed"];
const HOUR = 36e5;

// The window in which a breach should reach the Board.
const NOTIFY_WINDOW_HOURS = 72;

export default function BreachManagement() {
  const { notify } = useApp();
  const [rows, setRows] = useState([]);
  const [selected, setSelected] = useState(null);
  const [creating, setCreating] = useState(false);
  const [confirmReport, setConfirmReport] = useState(false);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    severity: "medium",
    affected_users: 0,
    categories: "",
    description: "",
    remediation: "",
  });

  const load = () => getBreaches().then(setRows);

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    if (selected) setForm({ ...selected });
  }, [selected]);

  const hoursSince = (iso) => Math.floor((Date.now() - new Date(iso)) / HOUR);

  const stats = useMemo(() => {
    const open = rows.filter((b) => b.status !== "closed");
    const unreported = open.filter((b) => !b.reported_at);
    const breachingWindow = unreported.filter((b) => hoursSince(b.detected_at) > NOTIFY_WINDOW_HOURS);
    return {
      open: open.length,
      unreported: unreported.length,
      breaching: breachingWindow.length,
      affected: rows.reduce((a, b) => a + (b.affected_users || 0), 0),
    };
  }, [rows]);

  const save = async (patch = {}) => {
    setBusy(true);
    try {
      const payload = { ...form, ...patch, affected_users: Number(form.affected_users) || 0 };
      if (selected) payload.id = selected.id;
      const row = await saveBreach(payload);
      await load();
      setSelected(row);
      setCreating(false);
      notify(selected ? "Breach record updated." : "Breach recorded.");
    } finally {
      setBusy(false);
    }
  };

  const reportToDpb = async () => {
    setBusy(true);
    try {
      await saveBreach({
        ...form,
        id: selected.id,
        status: "reported_to_dpb",
        reported_at: new Date().toISOString(),
      });
      await load();
      notify("Reported to the Data Protection Board and affected users notified.");
    } finally {
      setBusy(false);
      setConfirmReport(false);
      setSelected(null);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">Breach management</h1>
          <p className="text-sm text-muted">
            A personal data breach must be reported to the Data Protection Board and to every
            affected Data Principal.
          </p>
        </div>
        <button
          type="button"
          className="btn-primary"
          {...previewLock("breach", "Recording a breach")}
          onClick={() => {
            setSelected(null);
            setForm({ severity: "medium", affected_users: 0, categories: "", description: "", remediation: "" });
            setCreating(true);
          }}
        >
          Record a breach
        </button>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Open incidents" value={stats.open} tone="warning" />
        <StatCard label="Not yet reported" value={stats.unreported}
                  tone={stats.unreported > 0 ? "warning" : "neutral"} />
        <StatCard
          label={`Past the ${NOTIFY_WINDOW_HOURS}h window`}
          value={stats.breaching}
          tone={stats.breaching > 0 ? "danger" : "success"}
          hint={stats.breaching > 0 ? "Report immediately" : "All within window"}
        />
        <StatCard label="Data Principals affected" value={stats.affected.toLocaleString()} tone="neutral" />
      </div>

      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-line">
            <thead className="bg-canvas">
              <tr>
                <th className="th">Reference</th>
                <th className="th">Detected</th>
                <th className="th">Severity</th>
                <th className="th">Affected</th>
                <th className="th">Categories</th>
                <th className="th">Status</th>
                <th className="th">Reported</th>
                <th className="th sr-only">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {rows.length === 0 && (
                <tr><td className="td text-center text-muted" colSpan={8}>No breaches recorded.</td></tr>
              )}
              {rows.map((b) => {
                const late = !b.reported_at && hoursSince(b.detected_at) > NOTIFY_WINDOW_HOURS;
                return (
                  <tr key={b.id} onClick={() => { setCreating(false); setSelected(b); }}
                      className={`cursor-pointer hover:bg-canvas ${late ? "bg-danger/5" : ""}`}>
                    <td className="td font-mono text-xs">{b.reference}</td>
                    <td className="td text-xs text-muted">
                      {new Date(b.detected_at).toLocaleString()}
                      <div className={late ? "font-semibold text-danger" : "text-muted"}>
                        {hoursSince(b.detected_at)}h ago
                      </div>
                    </td>
                    <td className="td">
                      <span className={`tag capitalize ${
                        b.severity === "critical" || b.severity === "high" ? "border-danger/40 text-danger" : ""
                      }`}>
                        {b.severity}
                      </span>
                    </td>
                    <td className="td">{(b.affected_users || 0).toLocaleString()}</td>
                    <td className="td text-xs">{b.categories}</td>
                    <td className="td"><StatusBadge status={b.status} /></td>
                    <td className="td text-xs">
                      {b.reported_at ? (
                        <span className="text-success">
                          {new Date(b.reported_at).toLocaleDateString()}
                        </span>
                      ) : (
                        <span className={late ? "font-semibold text-danger" : "text-warning"}>
                          {late ? "OVERDUE" : "pending"}
                        </span>
                      )}
                    </td>
                    <td className="td"><span className="text-sm text-teal underline">View</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <SlideOver
        open={Boolean(selected) || creating}
        title={creating ? "Record a new breach" : selected ? `${selected.reference}` : ""}
        subtitle={
          selected && !creating
            ? `Detected ${hoursSince(selected.detected_at)}h ago`
            : "Log what happened as soon as you become aware of it"
        }
        onClose={() => {
          setSelected(null);
          setCreating(false);
        }}
        footer={
          <div className="flex flex-wrap items-center gap-3">
            <button type="button" className="btn-primary" onClick={() => save()} disabled={busy} {...previewLock("breach", "Saving a breach record")}>
              {busy ? "Saving…" : creating ? "Record breach" : "Save changes"}
            </button>
            {selected && !creating && !selected.reported_at && (
              <button type="button" className="btn-danger" onClick={() => setConfirmReport(true)} {...previewLock("breach", "Reporting to the Board")}>
                Report to DPB &amp; notify users
              </button>
            )}
          </div>
        }
      >
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="b-severity">Severity</label>
              <select id="b-severity" className="input" value={form.severity}
                      onChange={(e) => setForm({ ...form, severity: e.target.value })}>
                {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
            <div>
              <label className="label" htmlFor="b-affected">Data Principals affected</label>
              <input id="b-affected" type="number" min="0" className="input" value={form.affected_users}
                     onChange={(e) => setForm({ ...form, affected_users: e.target.value })} />
            </div>
            <div className="sm:col-span-2">
              <label className="label" htmlFor="b-categories">Data categories involved</label>
              <input id="b-categories" className="input" value={form.categories}
                     placeholder="e.g. Contact Data, Identity Data"
                     onChange={(e) => setForm({ ...form, categories: e.target.value })} />
            </div>
          </div>

          {selected && !creating && (
            <div>
              <label className="label" htmlFor="b-status">Status</label>
              <select id="b-status" className="input" value={form.status || "investigating"}
                      onChange={(e) => setForm({ ...form, status: e.target.value })}>
                {STATUSES.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
              </select>
            </div>
          )}

          <div>
            <label className="label" htmlFor="b-description">What happened</label>
            <textarea id="b-description" className="input min-h-[100px]" value={form.description}
                      onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </div>

          <div>
            <label className="label" htmlFor="b-remediation">Remediation taken</label>
            <textarea id="b-remediation" className="input min-h-[100px]" value={form.remediation}
                      onChange={(e) => setForm({ ...form, remediation: e.target.value })} />
          </div>

          {selected && !creating && (
            <div className={`rounded-lg border p-3 text-sm ${
              selected.reported_at
                ? "border-success/40 bg-success/5"
                : hoursSince(selected.detected_at) > NOTIFY_WINDOW_HOURS
                  ? "border-danger/40 bg-danger/5"
                  : "border-warning/40 bg-warning/5"
            }`}>
              <p className="font-medium text-ink">Notification duty</p>
              <p className="mt-1 text-muted">
                {selected.reported_at
                  ? `Reported to the Board on ${new Date(selected.reported_at).toLocaleString()}. Affected users were notified.`
                  : `Detected ${hoursSince(selected.detected_at)} hours ago. Target is within ${NOTIFY_WINDOW_HOURS} hours of becoming aware.`}
              </p>
            </div>
          )}
        </div>
      </SlideOver>

      <ConfirmModal
        open={confirmReport}
        title="Report this breach to the Data Protection Board?"
        body={`${selected?.reference} — ${(selected?.affected_users || 0).toLocaleString()} Data Principals affected.`}
        consequences={[
          "A breach notification is filed with the Data Protection Board of India.",
          `All ${(selected?.affected_users || 0).toLocaleString()} affected Data Principals are notified individually.`,
          "The filing is timestamped and written to the immutable audit trail.",
          "This cannot be withdrawn once submitted.",
        ]}
        confirmLabel="File the notification"
        busy={busy}
        onCancel={() => setConfirmReport(false)}
        onConfirm={reportToDpb}
      />
    </div>
  );
}
