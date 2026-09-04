// ============================================================================
// Data map for one rights request (/admin/dsar/:id/data-map)
//
// Where this person's data lives across every connected system, and the place
// an erasure is actually carried out.
//
// SCOPED TO THE REQUEST, ON PURPOSE. The route needs a request id and the API
// has no "look up any person" call, so there is no way to reach this screen
// except through a request that names them. That is what keeps it from becoming
// a customer-data browser with a filter on it.
//
// METADATA, NOT CONTENT. Tables, row counts, the matched column, and the
// categories the column names suggest — never a value. Everything an admin
// needs to answer the three questions a deletion actually turns on:
//
//   is this the right person   the matched identifier and column are shown
//   how much is affected      row counts per table
//   must any of it be kept    Financial / Government ID / Health are flagged,
//                             because those are the ones statute protects
//
// A rights request authorises acting on somebody's data, not reading it. An
// admin who opens a full customer record because a request arrived is
// processing it for a new purpose, which is the thing DPDP restricts.
//
// WHAT ERASURE DOES HERE. It masks the identifying columns and leaves the rest
// of the row. An order's amount is the company's financial record and often one
// they are legally required to keep; removing the person from it is erasure,
// removing the row is destroying a statutory record on their behalf. The screen
// says so, per table, before anything is pressed.
// ============================================================================
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  STATUTORY_CATEGORIES,
  dataMap as fetchMap,
  eraseAcrossSystems,
} from "../../api/dataMap";
import { useApp } from "../../context/AppContext";

function CategoryTag({ name }) {
  const statutory = STATUTORY_CATEGORIES.includes(name);
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs ${
        statutory
          ? "border-warning/50 bg-warning/10 text-ink"
          : "border-line bg-surface text-muted"
      }`}
      title={
        statutory
          ? "Statute often requires this category to be retained — check before erasing."
          : undefined
      }
    >
      {statutory && (
        <span className="h-2 w-2 rounded-full bg-warning" aria-hidden="true" />
      )}
      {name}
    </span>
  );
}

export default function DsarDataMap() {
  const { requestId } = useParams();
  const navigate = useNavigate();
  const { notify } = useApp();

  const [map, setMap] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState("");
  const [excluded, setExcluded] = useState(() => new Set());
  const [result, setResult] = useState(null);

  const load = useCallback(async () => {
    setError("");
    try {
      setMap(await fetchMap(requestId));
    } catch (e) {
      setError(e.message);
    }
  }, [requestId]);

  useEffect(() => {
    load();
  }, [load]);

  if (error) {
    return (
      <div className="space-y-4">
        <div className="rounded-lg border border-danger/50 bg-danger/10 p-4 text-sm text-ink">
          {error}
        </div>
        <Link to="/admin/dsar" className="btn-secondary">Back to the queue</Link>
      </div>
    );
  }

  if (!map) {
    return (
      <p className="text-sm text-muted">
        Searching the connected systems for this person&hellip; this queries each
        one directly, so it can take a few seconds.
      </p>
    );
  }

  const { request, person, systems, total_rows, statutory_warning, searched_by } = map;
  const isErasure = request.type === "erasure";
  const blocked = person.legal_hold;

  const allKeys = systems.flatMap((s) =>
    (s.findings || []).map((f) => `${s.connection_id}:${f.table}`),
  );
  const selected = allKeys.filter((k) => !excluded.has(k));

  const toggle = (key) => {
    setExcluded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const erase = async () => {
    setBusy(true);
    try {
      const out = await eraseAcrossSystems(requestId, {
        confirmReference: confirm,
        // Only send `only` when something was actually excluded — otherwise an
        // empty exclusion set and "erase everything" are the same intent, and
        // sending a list is just a chance to get it wrong.
        only: excluded.size > 0 ? selected : undefined,
      });
      setResult(out);
      notify(
        out.all_succeeded
          ? `${out.rows_masked} row(s) masked across the connected systems.`
          : `${out.rows_masked} row(s) masked, ${out.failures} table(s) failed.`,
        out.all_succeeded ? "success" : "error",
      );
      await load();
      setConfirm("");
    } catch (e) {
      notify(e.message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">
            Where this person&rsquo;s data is
          </h1>
          <p className="text-sm text-muted">
            {request.reference} · {request.type} request ·{" "}
            {total_rows} row{total_rows === 1 ? "" : "s"} found across{" "}
            {systems.length} connected system{systems.length === 1 ? "" : "s"}
          </p>
        </div>
        <button
          type="button"
          className="btn-ghost text-sm"
          onClick={() => navigate("/admin/dsar")}
        >
          Back to the queue
        </button>
      </div>

      {/* Who was searched for, and by what. A wrong match must be visible
          before anything is erased, not inferred afterwards. */}
      <div className="card p-4 text-sm">
        <p className="font-medium text-ink">Matching on</p>
        <dl className="mt-2 grid gap-x-6 gap-y-1 sm:grid-cols-3">
          {["email", "phone", "external_id"].map((k) => (
            <div key={k} className="flex gap-2">
              <dt className="text-muted">{k.replace("_", " ")}</dt>
              <dd className="font-mono text-xs text-ink">
                {person[k] || <span className="text-muted">not on record</span>}
              </dd>
            </div>
          ))}
        </dl>
        <p className="mt-2 text-xs text-muted">
          Searched by: {searched_by.join(", ") || "nothing — this person has no "}
          {searched_by.length === 0 && "identifier on record"}. Only identifiers
          that exist are searched, so a missing phone number does not match every
          row with a blank phone column.
        </p>
      </div>

      {/* Metadata-only, said out loud. Somebody will otherwise wonder why they
          cannot see the actual rows. */}
      <div className="card border-info/40 bg-info/5 p-4 text-sm">
        <p className="text-ink">
          This shows <strong>where</strong> the data is, not what it says. Row
          counts, column names and categories — never values.
        </p>
        <p className="mt-1 text-xs text-muted">
          A rights request lets you act on someone&rsquo;s data, not read it.
          Everything needed to erase safely is here: which identifier matched,
          how much is affected, and whether any of it is a category statute
          protects.
        </p>
      </div>

      {statutory_warning.length > 0 && (
        <div className="card border-warning/50 bg-warning/10 p-4 text-sm">
          <p className="font-semibold text-ink">
            Some of this may have to be kept
          </p>
          <p className="mt-1 text-muted">
            Tables below hold {statutory_warning.join(", ")} data. DPDP
            §12(3) allows refusing erasure where another law requires retention —
            RBI record-keeping, tax rules. Uncheck any table you must retain, or
            reject the request with that as the recorded reason.
          </p>
        </div>
      )}

      {blocked && (
        <div className="card border-danger/50 bg-danger/10 p-4 text-sm">
          <p className="font-semibold text-ink">This person is under a legal hold</p>
          <p className="mt-1 text-muted">
            {person.legal_hold_reason || "No reason recorded."} Erasure is
            refused while the hold stands — lift it first, or reject the request
            with the hold as the reason.
          </p>
        </div>
      )}

      {/* ------------------------------------------------- the systems ---- */}
      {systems.map((s) => (
        <section key={s.connection_id} className="card overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-5 py-3">
            <div>
              <p className="font-medium text-ink">
                {s.label}{" "}
                <span className="text-xs text-muted">· {s.connector_label}</span>
              </p>
              <p className="text-xs text-muted">
                {s.ok
                  ? `${s.tables_scanned} table(s) inspected, ${s.total_rows} row(s) matched`
                  : "not searched"}
              </p>
            </div>
            {s.truncated && (
              <span className="text-xs text-warning">
                only the first tables were inspected
              </span>
            )}
          </div>

          {!s.ok ? (
            // "We did not look" is not "there is nothing here", and an admin
            // must not read one as the other.
            <p className="px-5 py-4 text-sm text-ink">
              <span className="font-medium text-danger">Unknown. </span>
              {s.error}
            </p>
          ) : s.findings.length === 0 ? (
            <p className="px-5 py-4 text-sm text-muted">
              No rows matched this person here.
            </p>
          ) : (
            <ul className="divide-y divide-line">
              {s.findings.map((f) => {
                const key = `${s.connection_id}:${f.table}`;
                const included = !excluded.has(key);
                return (
                  <li key={key} className="px-5 py-4">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="font-mono text-sm text-ink">{f.table}</p>
                        <p className="mt-0.5 text-xs text-muted">
                          {f.rows} row{f.rows === 1 ? "" : "s"} · matched{" "}
                          {f.matched_identifier} on{" "}
                          <code className="font-mono">{f.matched_column}</code>
                        </p>
                        <div className="mt-2 flex flex-wrap gap-1.5">
                          {f.categories.map((c) => (
                            <CategoryTag key={c} name={c} />
                          ))}
                        </div>
                      </div>

                      {isErasure && !blocked && (
                        <label className="flex shrink-0 items-center gap-2 text-xs text-muted">
                          <input
                            type="checkbox"
                            checked={included}
                            onChange={() => toggle(key)}
                          />
                          erase this table
                        </label>
                      )}
                    </div>

                    {/* What a mask would and would not touch, per table. The
                        second half matters more: an admin should see that the
                        amount survives before they worry about it. */}
                    <div className="mt-3 rounded-lg border border-line bg-canvas p-3 text-xs">
                      <p className="text-ink">
                        <span className="text-muted">Would be cleared: </span>
                        {f.would_mask.length > 0 ? (
                          <span className="font-mono">
                            {f.would_mask.join(", ")}
                          </span>
                        ) : (
                          <span className="text-muted">
                            nothing — no identifying column here
                          </span>
                        )}
                      </p>
                      <p className="mt-1 text-ink">
                        <span className="text-muted">Would be left alone: </span>
                        <span className="font-mono">
                          {f.columns
                            .filter((c) => !f.would_mask.includes(c))
                            .join(", ") || "—"}
                        </span>
                      </p>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      ))}

      {/* ------------------------------------------------------ erase ---- */}
      {isErasure && !blocked && allKeys.length > 0 && (
        <section className="card border-danger/40 p-5">
          <h2 className="font-semibold text-ink">Erase from these systems</h2>
          <p className="mt-1 text-sm text-muted">
            Identifying columns are cleared and the rest of each row is left, so
            a financial record survives without the person in it. This cannot be
            undone.
          </p>
          <p className="mt-2 text-sm text-ink">
            {selected.length} of {allKeys.length} table(s) selected.
            {excluded.size > 0 && (
              <span className="text-muted">
                {" "}
                {excluded.size} will be left untouched.
              </span>
            )}
          </p>

          <div className="mt-4 max-w-sm">
            <label className="label" htmlFor="confirm-ref">
              Type {request.reference} to confirm
            </label>
            <input
              id="confirm-ref"
              className="input font-mono"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder={request.reference}
              autoComplete="off"
            />
          </div>

          <button
            type="button"
            className="btn-primary mt-4 bg-danger hover:bg-danger/90"
            disabled={
              busy ||
              selected.length === 0 ||
              confirm.trim().toUpperCase() !== request.reference.toUpperCase()
            }
            onClick={erase}
          >
            {busy ? "Erasing…" : `Erase from ${selected.length} table(s)`}
          </button>

          <p className="mt-3 text-xs text-muted">
            This does not close the request. The person still has to be told, and
            whether everything in scope was reached is your call to record.
          </p>
        </section>
      )}

      {!isErasure && (
        <div className="card p-4 text-sm text-muted">
          This is a <strong className="text-ink">{request.type}</strong> request,
          so nothing is erased from here. The map above shows what would need to
          be collected to answer it.
        </div>
      )}

      {/* ----------------------------------------------------- result ---- */}
      {result && (
        <section className="card overflow-hidden">
          <div className="border-b border-line px-5 py-3">
            <h2 className="font-semibold text-ink">What was done</h2>
            <p className="text-xs text-muted">
              Also written to this request&rsquo;s timeline, one entry per table,
              so the receipt sits with the request that caused it.
            </p>
          </div>
          <ul className="divide-y divide-line text-sm">
            {result.outcomes.map((o, i) => (
              <li key={`${o.connection}-${o.table}-${i}`} className="px-5 py-3">
                <span
                  className={`mr-2 inline-block h-2 w-2 rounded-full ${
                    o.skipped ? "bg-muted" : o.ok ? "bg-success" : "bg-danger"
                  }`}
                  aria-hidden="true"
                />
                <span className="text-muted">{o.connection}</span>{" "}
                <span className="font-mono text-ink">{o.table}</span>{" "}
                {o.skipped ? (
                  <span className="text-muted">left alone ({o.skipped})</span>
                ) : o.ok ? (
                  <span className="text-ink">
                    {o.rows_affected} row(s) masked
                    {o.columns_masked.length > 0 &&
                      ` — ${o.columns_masked.join(", ")}`}
                  </span>
                ) : (
                  <span className="text-danger">{o.error}</span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
