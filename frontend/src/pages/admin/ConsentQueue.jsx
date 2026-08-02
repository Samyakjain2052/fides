// ============================================================================
// Consent Validation Queue (/admin/consent-validation)
// What a Data Fiduciary calls before processing: "do I have valid consent for
// this user and this purpose right now?"
//
// Named ConsentQueue.jsx to match the brief's file structure, routed at
// /admin/consent-validation to match its screen list.
// ============================================================================
import { useEffect, useState } from "react";
import {
  bulkValidate,
  getValidationLog,
  MOCK_NOTICES,
  MOCK_USERS_ADMIN,
  validateConsent,
} from "../../api";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";

export default function ConsentQueue() {
  const { notify } = useApp();
  const [userId, setUserId] = useState("u001");
  const [purposeId, setPurposeId] = useState("n1");
  const [result, setResult] = useState(null);
  const [log, setLog] = useState([]);
  const [busy, setBusy] = useState(false);
  const [bulkResults, setBulkResults] = useState([]);

  const loadLog = () => getValidationLog().then(setLog);

  useEffect(() => {
    loadLog();
  }, []);

  const check = async () => {
    setBusy(true);
    try {
      const res = await validateConsent({ userId, purposeId });
      setResult(res);
      await loadLog();
    } finally {
      setBusy(false);
    }
  };

  const runBulk = async () => {
    setBusy(true);
    try {
      const pairs = MOCK_USERS_ADMIN.filter((u) => u.role === "data_principal").flatMap((u) =>
        MOCK_NOTICES.map((n) => ({ userId: u.id, purposeId: n.id }))
      );
      const res = await bulkValidate(pairs);
      setBulkResults(res);
      await loadLog();
      notify(`${res.length} consent checks completed.`);
    } finally {
      setBusy(false);
    }
  };

  const today = new Date().toISOString().slice(0, 10);
  const todayLog = log.filter((l) => l.checked_at.slice(0, 10) === today);

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">Consent validation</h1>
        <p className="text-sm text-muted">
          Check that consent is valid before processing. Every check is logged.
        </p>
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <section className="card p-5">
          <h2 className="font-semibold text-ink">Single check</h2>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="v-user">User ID</label>
              <input id="v-user" className="input" list="v-users" value={userId}
                     onChange={(e) => setUserId(e.target.value)} />
              <datalist id="v-users">
                {MOCK_USERS_ADMIN.map((u) => (
                  <option key={u.id} value={u.id}>{u.name}</option>
                ))}
              </datalist>
            </div>
            <div>
              <label className="label" htmlFor="v-purpose">Purpose ID</label>
              <select id="v-purpose" className="input" value={purposeId}
                      onChange={(e) => setPurposeId(e.target.value)}>
                {MOCK_NOTICES.map((n) => (
                  <option key={n.id} value={n.id}>{n.id} — {n.purpose}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="mt-4 flex flex-wrap gap-3">
            <button type="button" className="btn-primary" onClick={check} disabled={busy}>
              {busy ? "Checking…" : "Validate"}
            </button>
            <button type="button" className="btn-secondary" onClick={runBulk} disabled={busy}>
              Bulk validate all users × purposes
            </button>
          </div>

          {result && (
            <div className="mt-5 rounded-lg border border-line bg-canvas p-4">
              <div className="flex items-center gap-3">
                <StatusBadge status={result.result === "valid" ? "valid" : result.result} />
                <span className="text-sm text-muted">
                  {userId} / {purposeId}
                </span>
              </div>
              <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-3">
                <div>
                  <dt className="text-xs text-muted">Purpose alignment</dt>
                  <dd className="text-ink">{result.purpose_alignment}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">Timestamp validity</dt>
                  <dd className="text-ink">{result.timestamp_valid ? "valid" : "no record"}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">Consent status</dt>
                  <dd className="text-ink">{result.consent_status}</dd>
                </div>
              </dl>
              <p className="mt-3 text-xs text-muted">
                {result.result === "valid"
                  ? "You may process this data for this purpose."
                  : "Do not process. Processing without valid consent is a breach of the DPDP Act."}
              </p>
            </div>
          )}

          {bulkResults.length > 0 && (
            <div className="mt-5">
              <p className="text-sm font-semibold text-ink">
                Bulk result ({bulkResults.length} checks)
              </p>
              <div className="mt-2 max-h-56 overflow-y-auto rounded-lg border border-line">
                <table className="min-w-full divide-y divide-line text-sm">
                  <tbody className="divide-y divide-line">
                    {bulkResults.map((b, i) => (
                      <tr key={i}>
                        <td className="td font-mono text-xs">{b.userId}</td>
                        <td className="td font-mono text-xs">{b.purposeId}</td>
                        <td className="td"><StatusBadge status={b.result === "valid" ? "valid" : b.result} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </section>

        <section className="card p-5">
          <div className="flex items-center justify-between gap-2">
            <h2 className="font-semibold text-ink">API log — today</h2>
            <span className="tag">{todayLog.length} calls</span>
          </div>
          <p className="text-xs text-muted">Every validation request made today.</p>

          <div className="mt-4 overflow-x-auto">
            <table className="min-w-full divide-y divide-line">
              <thead className="bg-canvas">
                <tr>
                  <th className="th">Time</th>
                  <th className="th">User</th>
                  <th className="th">Purpose</th>
                  <th className="th">Result</th>
                  <th className="th">Caller</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {log.length === 0 && (
                  <tr><td className="td text-center text-muted" colSpan={5}>No calls recorded.</td></tr>
                )}
                {log.slice(0, 25).map((l) => (
                  <tr key={l.id}>
                    <td className="td text-xs text-muted">
                      {new Date(l.checked_at).toLocaleTimeString()}
                    </td>
                    <td className="td font-mono text-xs">{l.user_id}</td>
                    <td className="td font-mono text-xs">{l.purpose_id}</td>
                    <td className="td"><StatusBadge status={l.result === "valid" ? "valid" : l.result} /></td>
                    <td className="td text-xs text-muted">{l.caller}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  );
}
