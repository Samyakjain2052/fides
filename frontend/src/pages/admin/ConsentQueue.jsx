// ============================================================================
// Consent validation (/admin/consent-validation)
//
// The question a customer's systems ask in real time, before processing: "do I
// have valid consent for this person and this purpose *right now*?"
//
// Rewritten against the real API. It previously used the mock `validateConsent`,
// `bulkValidate`, `getValidationLog`, `MOCK_NOTICES` and `MOCK_USERS_ADMIN` while
// the `consent` module was flagged live — so a screen labelled live answered a
// compliance question with invented data.
//
// Three changes worth naming:
//
//   * **The people and purposes come from this workspace.** The old version had
//     hardcoded `u001` / `n1` defaults that referred to mock rows.
//   * **The "validation log" is the audit chain**, filtered to `consent.validated`.
//     There is no separate log table and there should not be: every check is
//     already recorded in the tamper-evident chain, and a second list that could
//     disagree with it would be worse than no list.
//   * **The cartesian bulk check is gone.** It ran every mock user against every
//     mock notice — cheap against arrays, and N×M HTTP calls against real data.
//     Checking every purpose for *one* person is the question somebody actually
//     asks ("what may I do with this person's data?"), so that is what this does.
// ============================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import { checkConsent, listPurposes } from "../../api/consent";
import { auditTrail } from "../../api/audit";
import { apiFetch } from "../../api/auth";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";

/** The server's answer, rendered without softening it. */
function Verdict({ result }) {
  if (!result) return null;
  return (
    <div
      className={`rounded-lg border p-4 ${
        result.allowed
          ? "border-success/50 bg-success/5"
          : "border-danger/50 bg-danger/5"
      }`}
    >
      <div className="flex flex-wrap items-center gap-3">
        <span
          className={`text-lg font-semibold ${
            result.allowed ? "text-success" : "text-danger"
          }`}
        >
          {result.allowed ? "Allowed" : "Not allowed"}
        </span>
        <StatusBadge status={result.status} />
        <span className="font-mono text-xs text-muted">{result.purpose}</span>
      </div>
      {/* Why not. The server supplies this and it is the only part a developer
          integrating against the API actually needs. */}
      {result.reason && <p className="mt-2 text-sm text-ink">{result.reason}</p>}
      <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-3">
        <div>
          <dt className="text-muted">Given</dt>
          <dd className="text-ink">
            {result.given_at ? new Date(result.given_at).toLocaleString() : "—"}
          </dd>
        </div>
        <div>
          <dt className="text-muted">Withdrawn</dt>
          <dd className="text-ink">
            {result.withdrawn_at
              ? new Date(result.withdrawn_at).toLocaleString()
              : "—"}
          </dd>
        </div>
        <div>
          <dt className="text-muted">Expires</dt>
          <dd className="text-ink">
            {result.expires_at ? new Date(result.expires_at).toLocaleString() : "—"}
          </dd>
        </div>
      </dl>
    </div>
  );
}

export default function ConsentQueue() {
  const { notify } = useApp();

  const [principals, setPrincipals] = useState([]);
  const [purposes, setPurposes] = useState([]);
  const [principalId, setPrincipalId] = useState("");
  const [purposeKey, setPurposeKey] = useState("");
  const [result, setResult] = useState(null);
  const [matrix, setMatrix] = useState(null);
  const [log, setLog] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const loadLog = useCallback(async () => {
    try {
      // The audit chain is the log. Filtered to the validation action.
      const page = await auditTrail({ action: "consent.validated", limit: 25 });
      setLog(page.rows);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    Promise.all([apiFetch("/principals"), listPurposes()])
      .then(([people, purps]) => {
        setPrincipals(people);
        setPurposes(purps);
        if (people[0]) setPrincipalId(people[0].id);
        if (purps[0]) setPurposeKey(purps[0].key);
      })
      .catch((e) => setError(e.message));
    loadLog();
  }, [loadLog]);

  const selectedPerson = useMemo(
    () => principals.find((p) => p.id === principalId) || null,
    [principals, principalId],
  );

  const check = async () => {
    setBusy(true);
    setError("");
    setMatrix(null);
    try {
      setResult(await checkConsent({ principalId, purposeKey }));
      await loadLog();
    } catch (e) {
      setError(e.message);
      setResult(null);
    } finally {
      setBusy(false);
    }
  };

  /**
   * Every purpose, for one person.
   *
   * Sequential rather than parallel: each check writes to the audit chain, which
   * takes a per-tenant advisory lock, so firing them at once would just make them
   * queue behind each other with less readable failures.
   */
  const checkAll = async () => {
    setBusy(true);
    setError("");
    setResult(null);
    const out = [];
    try {
      for (const p of purposes) {
        out.push({ purpose: p, result: await checkConsent({ principalId, purposeKey: p.key }) });
      }
      setMatrix(out);
      await loadLog();
      const allowed = out.filter((r) => r.result.allowed).length;
      notify(`${allowed} of ${out.length} purposes are permitted for this person.`);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">Consent validation</h1>
        <p className="text-sm text-muted">
          What a system asks before processing. Expiry is evaluated against the
          clock on every call, not by a nightly job — a sweep would leave a window
          in which an expired consent still reads as active, and processing in that
          window is unlawful.
        </p>
      </div>

      {error && (
        <div className="rounded-lg border border-danger/50 bg-danger/10 p-3 text-sm text-ink">
          {error}
        </div>
      )}

      {principals.length === 0 ? (
        <div className="card p-8 text-center">
          <p className="text-sm text-muted">
            No data principals in this workspace yet. Consent has to exist before
            there is anything to validate.
          </p>
        </div>
      ) : (
        <>
          <div className="card flex flex-wrap items-end gap-3 p-4">
            <div className="min-w-[16rem] flex-1">
              <label className="label" htmlFor="cv-person">Person</label>
              <select
                id="cv-person"
                className="input"
                value={principalId}
                onChange={(e) => setPrincipalId(e.target.value)}
              >
                {principals.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.email || p.external_id}
                  </option>
                ))}
              </select>
            </div>
            <div className="min-w-[14rem] flex-1">
              <label className="label" htmlFor="cv-purpose">Purpose</label>
              <select
                id="cv-purpose"
                className="input"
                value={purposeKey}
                onChange={(e) => setPurposeKey(e.target.value)}
              >
                {purposes.map((p) => (
                  <option key={p.id} value={p.key}>
                    {p.name} ({p.key})
                  </option>
                ))}
              </select>
            </div>
            <button
              type="button"
              className="btn-primary"
              onClick={check}
              disabled={busy || !principalId || !purposeKey}
            >
              {busy ? "Checking…" : "Check"}
            </button>
            <button
              type="button"
              className="btn-secondary"
              onClick={checkAll}
              disabled={busy || !principalId || purposes.length === 0}
              title="Runs one check per purpose for this person. Each is recorded in the audit chain."
            >
              Check every purpose
            </button>
          </div>

          <Verdict result={result} />

          {matrix && (
            <section className="card overflow-hidden">
              <div className="border-b border-line px-5 py-3">
                <h2 className="font-semibold text-ink">
                  What is permitted for {selectedPerson?.email || selectedPerson?.external_id}
                </h2>
                <p className="text-xs text-muted">
                  {matrix.filter((r) => r.result.allowed).length} of {matrix.length}{" "}
                  purposes permitted right now.
                </p>
              </div>
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-line">
                  <thead className="bg-canvas">
                    <tr>
                      <th className="th">Purpose</th>
                      <th className="th">Category</th>
                      <th className="th">Verdict</th>
                      <th className="th">Status</th>
                      <th className="th">Why</th>
                      <th className="th">Expires</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line">
                    {matrix.map(({ purpose, result: r }) => (
                      <tr key={purpose.id} className={r.allowed ? "" : "bg-danger/5"}>
                        <td className="td">
                          {purpose.name}
                          <span className="ml-1 font-mono text-xs text-muted">
                            {purpose.key}
                          </span>
                        </td>
                        <td className="td text-xs text-muted">{purpose.category}</td>
                        <td className="td">
                          <span
                            className={
                              r.allowed
                                ? "text-success font-medium"
                                : "text-danger font-medium"
                            }
                          >
                            {r.allowed ? "Allowed" : "No"}
                          </span>
                        </td>
                        <td className="td">
                          <StatusBadge status={r.status} />
                        </td>
                        <td className="td text-xs text-muted">{r.reason || "—"}</td>
                        <td className="td text-xs text-muted">
                          {r.expires_at
                            ? new Date(r.expires_at).toLocaleDateString()
                            : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {/* -------------------------------------------------- the real log -- */}
          <section className="card overflow-hidden">
            <div className="border-b border-line px-5 py-3">
              <h2 className="font-semibold text-ink">Recent validations</h2>
              <p className="text-xs text-muted">
                From the audit chain, not a separate log. Every check is recorded
                there already, and a second list that could disagree with it would
                be worse than none.
              </p>
            </div>
            {log.length === 0 ? (
              <p className="px-5 py-8 text-center text-sm text-muted">
                No validation calls recorded yet.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-line">
                  <thead className="bg-canvas">
                    <tr>
                      <th className="th">Entry</th>
                      <th className="th">When</th>
                      <th className="th">Purpose</th>
                      <th className="th">Answer</th>
                      <th className="th">Asked by</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line">
                    {log.map((row) => (
                      <tr key={row.id}>
                        <td className="td font-mono text-xs">{row.log_id}</td>
                        <td className="td text-xs text-muted">
                          {new Date(row.timestamp).toLocaleString()}
                        </td>
                        <td className="td text-xs">{row.purpose_id || "—"}</td>
                        <td className="td">
                          {row.consent_status ? (
                            <StatusBadge status={row.consent_status} />
                          ) : (
                            <span className="text-xs text-muted">—</span>
                          )}
                        </td>
                        <td className="td text-xs text-muted">
                          {row.user_id}
                          <span className="ml-1">({row.initiator})</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
