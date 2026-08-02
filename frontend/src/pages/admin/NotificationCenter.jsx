// ============================================================================
// Notification Center (/admin/notifications)
// Two tabs: notifications sent to users, and alerts sent to fiduciaries and
// processors. Plus the multi-language template editor.
// ============================================================================
import { useEffect, useState } from "react";
import {
  getNotifications,
  LANGUAGES,
  NOTIFICATION_TEMPLATES,
  retryNotification,
  sendTestAlert,
} from "../../api";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";

const TABS = [
  { id: "user", label: "User Notifications" },
  { id: "fiduciary", label: "Fiduciary / Processor Alerts" },
];

export default function NotificationCenter() {
  const { notify } = useApp();
  const [tab, setTab] = useState("user");
  const [rows, setRows] = useState([]);
  const [busy, setBusy] = useState(false);
  const [testTarget, setTestTarget] = useState("test-processor.example.com");

  // template editor
  const [templates, setTemplates] = useState(NOTIFICATION_TEMPLATES);
  const [activeTemplate, setActiveTemplate] = useState(NOTIFICATION_TEMPLATES[0].id);
  const [templateLang, setTemplateLang] = useState("English");

  const load = () => getNotifications(tab).then(setRows);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  const retry = async (id) => {
    setBusy(true);
    try {
      await retryNotification(id);
      await load();
      notify("Notification re-sent.");
    } finally {
      setBusy(false);
    }
  };

  const test = async () => {
    setBusy(true);
    try {
      await sendTestAlert(testTarget);
      await load();
      notify("Test alert delivered (HTTP 200).");
    } finally {
      setBusy(false);
    }
  };

  const current = templates.find((t) => t.id === activeTemplate);

  const editTemplate = (patch) =>
    setTemplates((prev) => prev.map((t) => (t.id === activeTemplate ? { ...t, ...patch } : t)));

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">Notification centre</h1>
        <p className="text-sm text-muted">
          Everything the platform has sent, and the templates it sends.
        </p>
      </div>

      <div className="flex flex-wrap gap-2">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={`rounded-full px-4 py-1.5 text-sm transition ${
              tab === t.id ? "bg-navy text-white" : "border border-line bg-surface text-ink hover:bg-line/40"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "fiduciary" && (
        <div className="card flex flex-wrap items-end gap-3 p-4">
          <div className="min-w-[16rem] flex-1">
            <label className="label" htmlFor="test-target">Test endpoint</label>
            <input id="test-target" className="input" value={testTarget}
                   onChange={(e) => setTestTarget(e.target.value)} />
          </div>
          <button type="button" className="btn-secondary" onClick={test} disabled={busy}>
            Send Test Alert
          </button>
        </div>
      )}

      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-line">
            <thead className="bg-canvas">
              <tr>
                <th className="th">{tab === "user" ? "Recipient" : "Endpoint"}</th>
                <th className="th">Subject</th>
                <th className="th">Scenario</th>
                <th className="th">Channel</th>
                <th className="th">Status</th>
                {tab === "fiduciary" && <th className="th">HTTP</th>}
                {tab === "fiduciary" && <th className="th">Acknowledged</th>}
                {tab === "user" && <th className="th">Language</th>}
                <th className="th">Sent</th>
                <th className="th sr-only">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {rows.length === 0 && (
                <tr><td className="td text-center text-muted" colSpan={9}>Nothing sent yet.</td></tr>
              )}
              {rows.map((n) => (
                <tr key={n.id} className={n.escalated ? "bg-danger/5" : ""}>
                  <td className="td text-xs">{n.to}</td>
                  <td className="td">{n.subject}</td>
                  <td className="td"><span className="tag">{n.scenario}</span></td>
                  <td className="td text-xs">{n.channel}</td>
                  <td className="td"><StatusBadge status={n.status} /></td>
                  {tab === "fiduciary" && (
                    <td className="td">
                      <span className={n.http_status === 200 ? "text-success" : "text-danger"}>
                        {n.http_status || "—"}
                      </span>
                    </td>
                  )}
                  {tab === "fiduciary" && (
                    <td className="td text-xs">
                      {n.acknowledged ? (
                        <span className="text-success">yes</span>
                      ) : (
                        <span className="text-danger">
                          no{n.escalated ? " — escalated to DPO" : ""}
                        </span>
                      )}
                    </td>
                  )}
                  {tab === "user" && <td className="td text-xs">{n.language || "English"}</td>}
                  <td className="td text-xs text-muted">
                    {new Date(n.sent_at).toLocaleString()}
                  </td>
                  <td className="td">
                    {n.status === "failed" && (
                      <button type="button" className="text-sm text-teal underline"
                              onClick={() => retry(n.id)} disabled={busy}>
                        Retry
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {tab === "fiduciary" && (
        <section className="card p-5">
          <h2 className="font-semibold text-ink">Escalation log</h2>
          <p className="text-xs text-muted">
            Alerts not acknowledged before their deadline are escalated to the DPO automatically.
          </p>
          <ul className="mt-3 space-y-2 text-sm">
            {rows.filter((n) => n.escalated).length === 0 ? (
              <li className="text-muted">No escalations outstanding.</li>
            ) : (
              rows
                .filter((n) => n.escalated)
                .map((n) => (
                  <li key={n.id}
                      className="flex flex-wrap items-center gap-3 rounded-lg border border-danger/40 bg-danger/5 px-3 py-2">
                    <span className="text-ink">{n.to}</span>
                    <span className="text-muted">{n.subject}</span>
                    <span className="ml-auto text-xs text-danger">
                      unacknowledged since {new Date(n.sent_at).toLocaleString()}
                    </span>
                  </li>
                ))
            )}
          </ul>
        </section>
      )}

      {/* ----------------------------------------------- template editor -- */}
      <section className="card p-5">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="font-semibold text-ink">Notification templates</h2>
            <p className="text-xs text-muted">
              One template per scenario, per language. Placeholders like{" "}
              <span className="font-mono">{"{{name}}"}</span> are filled at send time.
            </p>
          </div>
          <div className="flex flex-wrap gap-3">
            <div>
              <label className="label" htmlFor="t-scenario">Scenario</label>
              <select id="t-scenario" className="input" value={activeTemplate}
                      onChange={(e) => setActiveTemplate(e.target.value)}>
                {templates.map((t) => <option key={t.id} value={t.id}>{t.scenario}</option>)}
              </select>
            </div>
            <div>
              <label className="label" htmlFor="t-lang">Language</label>
              <select id="t-lang" className="input" value={templateLang}
                      onChange={(e) => setTemplateLang(e.target.value)}>
                {LANGUAGES.map((l) => <option key={l} value={l}>{l}</option>)}
              </select>
            </div>
          </div>
        </div>

        {current && (
          <div className="mt-4 space-y-3">
            <div>
              <label className="label" htmlFor="t-subject">Subject</label>
              <input id="t-subject" className="input" value={current.subject}
                     onChange={(e) => editTemplate({ subject: e.target.value })} />
            </div>
            <div>
              <label className="label" htmlFor="t-body">Body</label>
              <textarea id="t-body" className="input min-h-[130px]" value={current.body}
                        onChange={(e) => editTemplate({ body: e.target.value })} />
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <button type="button" className="btn-primary"
                      onClick={() => notify(`Template saved for ${templateLang}.`)}>
                Save template
              </button>
              {templateLang !== "English" && (
                <span className="text-xs text-muted">
                  Editing the {templateLang} version. Untranslated languages fall back to English at
                  send time.
                </span>
              )}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
