// ============================================================================
// ActionItems — the per-system work on one rights request.
//
// A queue that says "this request is in progress" gives a DPO no way to find
// out which of fifteen systems is the one nobody has touched. This does, and
// that is the whole reason it exists: the statutory clock runs on the request,
// not on the item, so the slowest system is the entire obligation.
//
// The closing form is deliberately not a one-click "done". Closing requires
// picking an outcome and writing a sentence, including — especially — when
// nothing was found. "Searched, nothing matched" is a finding with a name and a
// timestamp on it; an item that simply disappears from a list is not.
// ============================================================================
import { useCallback, useEffect, useState } from "react";
import {
  OUTCOMES,
  actionItems as loadItems,
  addManualItem,
  assignItem,
  claimItem,
  closeItem,
  fanOut,
  recordThirdParty,
  reopenItem,
  skipItem,
} from "../../api/actionItems";

const STATUS_STYLE = {
  pending: "border-line bg-surface text-muted",
  claimed: "border-info/50 bg-info/10 text-ink",
  completed: "border-success/50 bg-success/10 text-ink",
  skipped: "border-line bg-canvas text-muted",
  failed: "border-danger/50 bg-danger/10 text-ink",
};

function StatusPill({ status }) {
  return (
    <span
      className={`rounded-full border px-2 py-0.5 text-[11px] uppercase tracking-wide ${
        STATUS_STYLE[status] || STATUS_STYLE.pending
      }`}
    >
      {status}
    </span>
  );
}

/** The close form for one item. Local state, so two open at once do not share. */
function CloseForm({ item, onClose, onCancel, busy }) {
  const [outcome, setOutcome] = useState("no_records_matched");
  const [attestation, setAttestation] = useState("");
  const [records, setRecords] = useState(0);
  const [basis, setBasis] = useState("");
  const [notes, setNotes] = useState("");

  const chosen = OUTCOMES.find((o) => o.id === outcome);
  const needsBasis = outcome === "retained";
  const nilResult = outcome === "no_records_matched";
  // Mirrors the server's contradiction check, so somebody finds out before
  // submitting rather than after.
  const contradiction = nilResult && Number(records) > 0;

  const ready =
    attestation.trim().length > 0 &&
    !contradiction &&
    (!needsBasis || basis.trim().length > 0);

  return (
    <div className="mt-3 space-y-3 rounded-lg border border-line bg-canvas p-3">
      <div>
        <label className="label" htmlFor={`outcome-${item.id}`}>
          What did you find?
        </label>
        <select
          id={`outcome-${item.id}`}
          className="input"
          value={outcome}
          onChange={(e) => setOutcome(e.target.value)}
        >
          {OUTCOMES.map((o) => (
            <option key={o.id} value={o.id}>
              {o.label}
            </option>
          ))}
        </select>
        {chosen && <p className="mt-1 text-xs text-muted">{chosen.hint}</p>}
      </div>

      {!nilResult && (
        <div>
          <label className="label" htmlFor={`records-${item.id}`}>
            How many records
          </label>
          <input
            id={`records-${item.id}`}
            type="number"
            min="0"
            className="input max-w-[140px]"
            value={records}
            onChange={(e) => setRecords(e.target.value)}
          />
        </div>
      )}

      {needsBasis && (
        <div>
          <label className="label" htmlFor={`basis-${item.id}`}>
            Which obligation requires you to keep it
          </label>
          <input
            id={`basis-${item.id}`}
            className="input"
            value={basis}
            onChange={(e) => setBasis(e.target.value)}
            placeholder="e.g. Section 128 Companies Act 2013 — books of account, 8 years"
          />
          <p className="mt-1 text-xs text-muted">
            Keeping data against a request is a legal claim. It needs a ground,
            not a shrug.
          </p>
        </div>
      )}

      <div>
        <label className="label" htmlFor={`attestation-${item.id}`}>
          Say what you did, in a sentence
        </label>
        <textarea
          id={`attestation-${item.id}`}
          className="input min-h-[70px]"
          value={attestation}
          onChange={(e) => setAttestation(e.target.value)}
          placeholder={
            nilResult
              ? "Searched by email and phone across all tables; no rows matched."
              : "Masked the email, phone and name columns on 3 rows."
          }
        />
        <p className="mt-1 text-xs text-muted">
          This is what an auditor reads. A dropdown value records that a button
          was pressed; this records that somebody looked.
        </p>
      </div>

      <div>
        <label className="label" htmlFor={`notes-${item.id}`}>
          Internal notes (optional)
        </label>
        <textarea
          id={`notes-${item.id}`}
          className="input min-h-[50px]"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
        <p className="mt-1 text-xs text-muted">
          Not sent to the requester — our record of handling, not part of the
          response.
        </p>
      </div>

      {contradiction && (
        <p className="rounded border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
          You have said nothing matched and also that {records} record(s) were
          found. One of those is wrong.
        </p>
      )}

      <div className="flex items-center gap-2">
        <button
          type="button"
          className="btn-primary"
          disabled={busy || !ready}
          onClick={() =>
            onClose({
              outcome,
              attestation: attestation.trim(),
              recordsFound: nilResult ? 0 : Number(records) || 0,
              basis: needsBasis ? basis.trim() : null,
              internalNotes: notes.trim() || null,
            })
          }
        >
          Close this item
        </button>
        <button type="button" className="btn-secondary" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

export default function ActionItems({ requestId, people = [], currentUserId }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [closing, setClosing] = useState(null);
  const [manual, setManual] = useState("");
  const [thirdParty, setThirdParty] = useState({ id: null, address: "" });

  const refresh = useCallback(async () => {
    try {
      setData(await loadItems(requestId));
    } catch (e) {
      setError(e.message);
    }
  }, [requestId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const run = async (fn) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      await refresh();
      return true;
    } catch (e) {
      setError(e.message);
      return false;
    } finally {
      setBusy(false);
    }
  };

  if (!data) return <p className="text-sm text-muted">Loading the work…</p>;

  const { items, summary } = data;

  return (
    <div className="space-y-4">
      {/* ------------------------------------------------------- summary -- */}
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          className="btn-secondary"
          disabled={busy}
          onClick={() => run(() => fanOut(requestId))}
        >
          {items.length ? "Refresh from connections" : "Create the work list"}
        </button>
        {items.length > 0 && (
          <p className="text-sm text-muted">
            {summary.completed} of {summary.total} done
            {summary.unassigned > 0 && (
              <>
                {" · "}
                <strong className="text-warning">
                  {summary.unassigned} with no owner
                </strong>
              </>
            )}
            {summary.nothing_found_in > 0 && (
              <> · {summary.nothing_found_in} held nothing</>
            )}
            {summary.records_found > 0 && (
              <> · {summary.records_found} record(s) affected</>
            )}
          </p>
        )}
      </div>

      {items.length === 0 && (
        <p className="text-sm text-muted">
          No work list yet. Creating one adds an item per connected system, each
          assigned to whoever owns it — so you can see which system nobody has
          touched rather than only that the request is &ldquo;in progress&rdquo;.
        </p>
      )}

      {error && (
        <p className="rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      {/* --------------------------------------------------------- items -- */}
      <ul className="space-y-3">
        {items.map((item) => (
          <li key={item.id} className="rounded-lg border border-line p-3">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <p className="font-medium text-ink">{item.system_label}</p>
                <p className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-muted">
                  <StatusPill status={item.status} />
                  {item.automated && (
                    <span className="rounded bg-line px-1.5 py-0.5 uppercase tracking-wide">
                      automatic
                    </span>
                  )}
                  {item.assignee_label ? (
                    <span>{item.assignee_label}</span>
                  ) : (
                    !item.automated && (
                      <span className="text-warning">no owner</span>
                    )
                  )}
                  {item.records_found > 0 && (
                    <span>{item.records_found} record(s)</span>
                  )}
                </p>
              </div>

              {item.is_open && (
                <div className="flex flex-wrap items-center gap-2">
                  <select
                    className="input max-w-[190px] py-1 text-sm"
                    value={item.assignee_user_id || ""}
                    onChange={(e) =>
                      run(() =>
                        assignItem(requestId, item.id, e.target.value || null),
                      )
                    }
                  >
                    <option value="">Unassigned</option>
                    {people.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.full_name || p.email}
                      </option>
                    ))}
                  </select>
                  {item.assignee_user_id !== currentUserId && (
                    <button
                      type="button"
                      className="btn-secondary py-1 text-sm"
                      disabled={busy}
                      onClick={() => run(() => claimItem(requestId, item.id))}
                    >
                      Take it
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn-primary py-1 text-sm"
                    onClick={() =>
                      setClosing(closing === item.id ? null : item.id)
                    }
                  >
                    {closing === item.id ? "Cancel" : "Close"}
                  </button>
                </div>
              )}
            </div>

            {/* ---------------------------------------------- conclusion -- */}
            {item.attestation && (
              <p className="mt-2 rounded border border-line bg-canvas px-3 py-2 text-sm text-ink">
                <span className="text-xs uppercase tracking-wide text-muted">
                  {(item.outcome || "").replace(/_/g, " ")}
                </span>
                <br />
                {item.attestation}
              </p>
            )}
            {item.skip_reason && (
              <p className="mt-2 text-xs text-muted">
                <strong className="text-ink">Ground:</strong> {item.skip_reason}
              </p>
            )}
            {item.failure_reason && (
              <p className="mt-2 text-xs text-danger">{item.failure_reason}</p>
            )}

            {(item.third_parties_notified || []).length > 0 && (
              <ul className="mt-2 space-y-1 text-xs text-muted">
                {item.third_parties_notified.map((t, i) => (
                  <li key={i}>
                    Told <span className="text-ink">{t.address}</span> on{" "}
                    {new Date(t.notified_at).toLocaleDateString()}
                    {t.note && <> — {t.note}</>}
                  </li>
                ))}
              </ul>
            )}

            {/* ------------------------------------------------- actions -- */}
            {closing === item.id && (
              <CloseForm
                item={item}
                busy={busy}
                onCancel={() => setClosing(null)}
                onClose={async (payload) => {
                  const ok = await run(() =>
                    closeItem(requestId, item.id, payload),
                  );
                  if (ok) setClosing(null);
                }}
              />
            )}

            <div className="mt-2 flex flex-wrap items-center gap-3 text-xs">
              {item.is_open && (
                <>
                  <button
                    type="button"
                    className="text-teal underline"
                    onClick={() => {
                      const reason = window.prompt(
                        "Why is this system being skipped? A skipped item with no reason is indistinguishable from a forgotten one.",
                      );
                      if (reason?.trim())
                        run(() => skipItem(requestId, item.id, reason.trim()));
                    }}
                  >
                    Skip with a reason
                  </button>
                  <button
                    type="button"
                    className="text-teal underline"
                    onClick={() =>
                      setThirdParty({
                        id: thirdParty.id === item.id ? null : item.id,
                        address: "",
                      })
                    }
                  >
                    Record telling somebody else
                  </button>
                </>
              )}
              {!item.is_open && (
                <button
                  type="button"
                  className="text-teal underline"
                  onClick={() => {
                    const reason = window.prompt(
                      "Why is this being reopened? The conclusion already recorded stays in the audit trail.",
                    );
                    if (reason?.trim())
                      run(() => reopenItem(requestId, item.id, reason.trim()));
                  }}
                >
                  Reopen
                </button>
              )}
            </div>

            {thirdParty.id === item.id && (
              <div className="mt-2 flex flex-wrap items-end gap-2">
                <div className="flex-1">
                  <label className="label" htmlFor={`tp-${item.id}`}>
                    Who was told
                  </label>
                  <input
                    id={`tp-${item.id}`}
                    className="input"
                    value={thirdParty.address}
                    onChange={(e) =>
                      setThirdParty({ ...thirdParty, address: e.target.value })
                    }
                    placeholder="privacy@processor.example"
                  />
                </div>
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={busy || !thirdParty.address.trim()}
                  onClick={async () => {
                    const ok = await run(() =>
                      recordThirdParty(requestId, item.id, {
                        address: thirdParty.address.trim(),
                      }),
                    );
                    if (ok) setThirdParty({ id: null, address: "" });
                  }}
                >
                  Record it
                </button>
                <p className="w-full text-xs text-muted">
                  Records that we told them, and when. It does not send
                  anything — what reaches a processor is a contractual matter.
                </p>
              </div>
            )}
          </li>
        ))}
      </ul>

      {/* --------------------------------------------------- manual item -- */}
      <div className="flex flex-wrap items-end gap-2 border-t border-line pt-3">
        <div className="flex-1">
          <label className="label" htmlFor="manual-system">
            Add a system we cannot reach
          </label>
          <input
            id="manual-system"
            className="input"
            value={manual}
            onChange={(e) => setManual(e.target.value)}
            placeholder="Payroll bureau, tape archive, a processor's own database…"
          />
        </div>
        <button
          type="button"
          className="btn-secondary"
          disabled={busy || !manual.trim()}
          onClick={async () => {
            const ok = await run(() =>
              addManualItem(requestId, { systemLabel: manual.trim() }),
            );
            if (ok) setManual("");
          }}
        >
          Add
        </button>
        <p className="w-full text-xs text-muted">
          In most organisations these are the majority of systems. Tracking only
          the ones we hold credentials for is tracking the easy part.
        </p>
      </div>
    </div>
  );
}
