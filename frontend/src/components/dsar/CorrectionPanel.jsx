// ============================================================================
// §12(1) correction, on the fulfilment screen.
//
// This replaced a paragraph that said "Correction is a manual workflow — make
// the change in the source system", a JSON dump of what was asked for, and a
// free-text box where somebody typed that they had done it. That box was the
// problem: "You name is changes as per the requirement" is a CLAIM, and a
// regulator asking "changed from what?" had nowhere to look.
//
// WHAT THE SCREEN HAS TO MAKE OBVIOUS
//
// Whether the change already happened. The backend applies a correction on its
// own when the person's stated current value matches exactly one column in
// exactly one system — so by the time a DPO opens this, the work is often done.
// A screen that looks identical either way would have them redo it by hand.
//
// Which targets are CONFIRMED and which are merely possible, and why. A
// candidate is not a weaker confirmation; it is a question. The reason it is
// not confirmed is the most useful sentence on the panel, so it is shown at
// full size rather than as a tooltip.
//
// That applying is a decision. Preview shows the stored value and writes
// nothing. Applying needs the reference typed back — the same guard the
// retention live run and connected erasure use, and for the same reason: there
// is no undo, so it must not follow from one unremarkable click.
// ============================================================================
import { useCallback, useEffect, useState } from "react";
import { autoCorrect, correctInSystem, correctionPlan } from "../../api/dsar";

/** The three §12(1) rights, in the words the person used. */
const ASKED = {
  correction: "says this is wrong",
  completion: "says this is missing",
  updating: "says this has changed",
};

function Pill({ tone, children }) {
  const tones = {
    confirmed: "bg-success/10 text-success border-success/30",
    candidate: "bg-warn/10 text-warn border-warn/30",
    applied: "bg-teal/10 text-teal border-teal/30",
  };
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${tones[tone] || tones.candidate}`}
    >
      {children}
    </span>
  );
}

/** One place the change could land. */
function Target({ hit, confirmed, reference, requestId, newValue, onDone }) {
  const [preview, setPreview] = useState(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const run = useCallback(
    async (dryRun) => {
      setBusy(true);
      setError("");
      try {
        const out = await correctInSystem(requestId, {
          connectionId: hit.connection_id,
          table: hit.table,
          column: hit.column,
          newValue,
          dryRun,
          confirmReference: dryRun ? undefined : typed,
        });
        if (dryRun) setPreview(out);
        else onDone(out);
      } catch (e) {
        setError(e.message || "That did not work.");
      } finally {
        setBusy(false);
      }
    },
    [hit, requestId, newValue, typed, onDone],
  );

  const armed = typed.trim().toUpperCase() === reference.toUpperCase();

  return (
    <div className="rounded-lg border border-line p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-mono text-sm text-ink">
          {hit.connection} · {hit.table}.{hit.column}
        </p>
        <Pill tone={confirmed ? "confirmed" : "candidate"}>
          {confirmed ? "Confirmed" : "Possible"}
        </Pill>
      </div>

      <dl className="mt-2 grid gap-1 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-muted">Stored now</dt>
          <dd className="font-mono text-ink">
            {(preview?.old_values ?? hit.current_values ?? []).join(", ") || (
              <span className="text-muted">(empty)</span>
            )}
          </dd>
        </div>
        <div>
          <dt className="text-muted">Would become</dt>
          <dd className="font-mono text-ink">{newValue}</dd>
        </div>
      </dl>

      {hit.rows_matched > 1 && (
        <p className="mt-2 text-xs text-warn">
          {hit.rows_matched} rows here match this person. A correction changes
          all of them.
        </p>
      )}

      {/* The most useful sentence on the panel: not "this is uncertain" but
          exactly what does not line up. */}
      {!confirmed && hit.why_not_confirmed && (
        <p className="mt-2 rounded-md bg-warn/5 px-2 py-1.5 text-xs text-warn">
          Not confirmed — {hit.why_not_confirmed}.
        </p>
      )}

      {error && <p className="mt-2 text-xs text-danger">{error}</p>}

      <div className="mt-3 flex flex-wrap items-end gap-2">
        <button
          type="button"
          className="btn-ghost text-sm"
          disabled={busy}
          onClick={() => run(true)}
        >
          {preview ? "Refresh what is stored" : "Show what is stored"}
        </button>

        <div className="flex items-end gap-2">
          <div>
            <label className="label" htmlFor={`ref-${hit.table}-${hit.column}`}>
              Type {reference} to apply
            </label>
            <input
              id={`ref-${hit.table}-${hit.column}`}
              className="input font-mono text-sm"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={reference}
              autoComplete="off"
            />
          </div>
          <button
            type="button"
            className="btn-primary text-sm"
            disabled={!armed || busy}
            onClick={() => run(false)}
          >
            {busy ? "Applying…" : "Apply this change"}
          </button>
        </div>
      </div>
      <p className="mt-1 text-xs text-muted">
        Writes to a live system. There is no undo.
      </p>
    </div>
  );
}

export default function CorrectionPanel({ request, requestId }) {
  const [plan, setPlan] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [applied, setApplied] = useState([]);
  const [retrying, setRetrying] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setPlan(await correctionPlan(requestId));
    } catch (e) {
      setError(e.message || "The plan could not be worked out.");
    } finally {
      setLoading(false);
    }
  }, [requestId]);

  useEffect(() => {
    load();
  }, [load]);

  const retry = useCallback(async () => {
    setRetrying(true);
    try {
      const out = await autoCorrect(requestId);
      setPlan(out);
      if (out.applied) setApplied((a) => [...a, out.applied]);
    } catch (e) {
      setError(e.message || "That did not work.");
    } finally {
      setRetrying(false);
    }
  }, [requestId]);

  const payload = request.correction_payload || {};
  const asked = payload.correction && typeof payload.correction === "object"
    ? payload.correction
    : payload;

  if (loading) {
    return <p className="text-sm text-muted">Reading the connected systems…</p>;
  }

  return (
    <div className="space-y-4">
      {/* What was actually asked for, in words rather than as a JSON dump. */}
      <div className="rounded-lg border border-line bg-canvas p-3 text-sm">
        <p className="text-ink">
          The person {ASKED[request.type] || "asked for a change"}:{" "}
          <span className="font-semibold">{asked.field || "—"}</span>
        </p>
        <p className="mt-1 text-muted">
          {asked.current ? (
            <>
              They say it currently reads{" "}
              <span className="font-mono text-ink">{asked.current}</span> and
              should be{" "}
              <span className="font-mono text-ink">{asked.corrected}</span>.
            </>
          ) : (
            <>
              They say nothing is stored, and it should be{" "}
              <span className="font-mono text-ink">{asked.corrected}</span>.
            </>
          )}
        </p>
      </div>

      {error && (
        <p className="rounded-md bg-danger/5 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      {/* Already done, and saying so plainly — otherwise a DPO redoes by hand
          work the server finished before they opened the page. */}
      {applied.length > 0 && (
        <div className="rounded-lg border border-teal/30 bg-teal/5 p-3">
          <Pill tone="applied">Applied</Pill>
          {applied.map((a, i) => (
            <p key={i} className="mt-2 font-mono text-sm text-ink">
              {a.table}.{a.column}: {(a.old_values || []).join(", ") || "(empty)"}{" "}
              → {a.new_value} · {a.rows_affected} row(s)
            </p>
          ))}
        </div>
      )}

      {plan?.confirmed?.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-semibold text-ink">
            Confirmed — the stored value is what they said it is
          </h3>
          {plan.confirmed.map((hit) => (
            <Target
              key={`${hit.connection_id}:${hit.table}:${hit.column}`}
              hit={hit}
              confirmed
              reference={plan.request}
              requestId={requestId}
              newValue={plan.new_value}
              onDone={(out) => setApplied((a) => [...a, out])}
            />
          ))}
        </div>
      )}

      {plan?.candidates?.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-semibold text-ink">
            Possible — these need you to decide
          </h3>
          {plan.candidates.map((hit) => (
            <Target
              key={`${hit.connection_id}:${hit.table}:${hit.column}`}
              hit={hit}
              confirmed={false}
              reference={plan.request}
              requestId={requestId}
              newValue={plan.new_value}
              onDone={(out) => setApplied((a) => [...a, out])}
            />
          ))}
        </div>
      )}

      {/* Nothing found is a real answer, not an error — and it means the work
          is somewhere this product cannot reach. */}
      {!plan?.confirmed?.length && !plan?.candidates?.length && (
        <div className="rounded-lg border border-line p-3 text-sm">
          <p className="text-ink">
            Nothing in the connected systems matches this field.
          </p>
          <p className="mt-1 text-muted">
            Either the field lives somewhere not connected here, or it is named
            differently from what the person called it. Add it as a system you
            cannot reach on the data map, and record what you changed there.
          </p>
        </div>
      )}

      {plan?.unreachable?.length > 0 && (
        <div className="rounded-lg border border-warn/30 bg-warn/5 p-3 text-sm">
          <p className="font-semibold text-warn">
            {plan.unreachable.length} system(s) could not be searched
          </p>
          <ul className="mt-1 space-y-0.5 text-warn">
            {plan.unreachable.map((u, i) => (
              <li key={i}>
                {u.connection}
                {u.table ? ` · ${u.table}` : ""} — {u.error}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-warn">
            This person's data may be in one of them. The deadline is still
            running.
          </p>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
        <button
          type="button"
          className="btn-secondary text-sm"
          onClick={load}
          disabled={loading}
        >
          Re-check the systems
        </button>
        <button
          type="button"
          className="btn-ghost text-sm"
          onClick={retry}
          disabled={retrying}
        >
          {retrying ? "Trying…" : "Apply automatically if unambiguous"}
        </button>
        {plan?.why_not_automatic && (
          <span className="text-xs text-muted">{plan.why_not_automatic}</span>
        )}
      </div>
    </div>
  );
}
