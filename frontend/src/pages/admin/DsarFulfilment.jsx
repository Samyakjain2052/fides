// ============================================================================
// Fulfilling one rights request (/admin/dsar/:requestId/fulfil)
//
// The screen where a tracked request becomes an answered one. Four things, in
// the order they actually happen: verify who is asking, talk to them, check
// what would be disclosed, then assemble and send it.
//
// WHAT THIS SCREEN CANNOT DO, AND WHY THAT IS THE POINT
//
// It cannot show you somebody's Aadhaar number, PAN, card number, account
// balance, health data, street address or postcode. Those arrive from the server
// already replaced with <SENSITIVE VALUE EXCLUDED> — not hidden in CSS, not
// filtered in the browser, absent from the response. A DPO with devtools open
// sees what the screen sees.
//
// It also cannot download the assembled package. No staff capability grants it.
// Verifying a disclosure means confirming the right person and the right shape,
// which the preview supports completely; reading the contents is a different act
// with a different justification, and a product where every DPO can read every
// customer's financial records by raising a request on their behalf has a much
// larger breach surface than it needs.
//
// ASSEMBLE AND SEND ARE TWO BUTTONS
//
// Because they are two decisions. A package can be assembled, checked against
// the preview, and found wrong — and "we prepared it" must never be able to read
// as "they received it" in an audit. The reference has to be typed back before
// assembly, the same guard the retention live run and the connected erasure use.
// ============================================================================
import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getRequest } from "../../api/dsar";
import {
  EXCLUDED_MARKER,
  WITHHELD_MARKER,
  assembleDisclosure,
  deliverDisclosure,
  downloadIdentityDocument,
  humanSize,
  previewDisclosure,
  reviewIdentity,
  saveBlob,
  uploadIdentityDocument,
} from "../../api/fulfilment";
import MessageThread from "../../components/common/MessageThread";
import { useApp } from "../../context/AppContext";

function Section({ step, title, subtitle, done, children }) {
  return (
    <section className="card overflow-hidden">
      <div className="flex items-start gap-3 border-b border-line px-5 py-4">
        <span
          className={[
            "mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold",
            done ? "bg-success text-white" : "bg-line text-muted",
          ].join(" ")}
          aria-hidden="true"
        >
          {done ? "✓" : step}
        </span>
        <div>
          <h2 className="font-semibold text-ink">{title}</h2>
          {subtitle && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
        </div>
      </div>
      <div className="p-5">{children}</div>
    </section>
  );
}

/** The redacted value cell. Marks excluded and withheld differently. */
function ValueCell({ value, classification }) {
  if (classification === "never") {
    return (
      <span
        className="font-mono text-xs text-muted"
        title="Credential material — a password hash or token. Not disclosed to anyone, including the person themselves: it is our credential about them, not information about them."
      >
        {WITHHELD_MARKER}
      </span>
    );
  }
  if (classification === "sensitive") {
    return (
      <span
        className="font-mono text-xs text-warning"
        title="Excluded from your view. Government ID, financial, health and street-level location values are not shown to staff — you can see that we hold this field, which is what verifying the disclosure requires. The person receives the real value."
      >
        {EXCLUDED_MARKER}
      </span>
    );
  }
  return (
    <span className="break-words font-mono text-xs text-ink">
      {value || <span className="text-muted">(empty)</span>}
    </span>
  );
}

export default function DsarFulfilment() {
  const { requestId } = useParams();
  const { notify } = useApp();

  const [request, setRequest] = useState(null);
  const [error, setError] = useState("");

  const [preview, setPreview] = useState(null);
  const [previewBusy, setPreviewBusy] = useState(false);

  const [confirm, setConfirm] = useState("");
  const [assembled, setAssembled] = useState(null);
  const [busy, setBusy] = useState(false);

  const [note, setNote] = useState("");
  const [refusal, setRefusal] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      setRequest(await getRequest(requestId));
    } catch (e) {
      setError(e.message);
    }
  }, [requestId]);

  useEffect(() => {
    load();
  }, [load]);

  const guard = async (fn, ok) => {
    setBusy(true);
    setError("");
    try {
      const result = await fn();
      if (ok) notify(ok);
      await load();
      return result;
    } catch (e) {
      setError(e.message);
      return null;
    } finally {
      setBusy(false);
    }
  };

  if (error && !request) {
    return (
      <div className="space-y-4">
        <Link to="/admin/dsar" className="text-sm text-teal underline">
          ← Back to the request queue
        </Link>
        <p className="rounded-lg border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">
          {error}
        </p>
      </div>
    );
  }

  if (!request) return <p className="text-sm text-muted">Loading…</p>;

  const isAccess = request.type === "access";
  const identityDone =
    Boolean(request.identity_reviewed_at) && !request.identity_rejection_reason;
  const closed = ["completed", "rejected", "cancelled"].includes(request.status);

  return (
    <div className="space-y-5">
      {/* -------------------------------------------------------- header -- */}
      <header className="space-y-1">
        <Link to="/admin/dsar" className="text-sm text-teal underline">
          ← Back to the request queue
        </Link>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold text-ink">{request.reference}</h1>
          <span className="rounded-full border border-line bg-surface px-2 py-0.5 text-xs uppercase tracking-wide text-muted">
            {request.type}
          </span>
          <span className="rounded-full border border-line bg-surface px-2 py-0.5 text-xs text-muted">
            {request.status}
          </span>
          {request.overdue && (
            <span className="rounded-full bg-danger px-2 py-0.5 text-xs font-semibold text-white">
              overdue
            </span>
          )}
        </div>
        <p className="text-sm text-muted">
          {request.principal_email || request.principal_ref || "no contact on record"}
          {request.deadline_at && (
            <> · due {new Date(request.deadline_at).toLocaleDateString()}</>
          )}
        </p>
        <p>
          <Link
            to={`/admin/dsar/${requestId}/data-map`}
            className="text-sm text-teal underline"
          >
            Where this person's data is →
          </Link>
        </p>
      </header>

      {error && (
        <p className="rounded-lg border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">
          {error}
        </p>
      )}

      {/* ------------------------------------------------------ identity -- */}
      <Section
        step="1"
        title="Who is asking"
        subtitle="Confirm identity before disclosing or erasing anything. Getting this wrong discloses one person's data to another."
        done={identityDone}
      >
        {request.identity_document_id ? (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                className="btn-secondary"
                onClick={async () => {
                  try {
                    saveBlob(
                      await downloadIdentityDocument(requestId),
                      `identity-${request.reference}`,
                    );
                  } catch (e) {
                    setError(e.message);
                  }
                }}
              >
                Open the document
              </button>
              <p className="text-xs text-muted">
                Every time this is opened it is recorded, with your name. The
                person whose ID it is has a right to know who looked.
              </p>
            </div>

            {identityDone ? (
              <p className="rounded-lg border border-success/40 bg-success/5 px-3 py-2 text-sm text-ink">
                Verified{" "}
                {request.identity_reviewed_at &&
                  new Date(request.identity_reviewed_at).toLocaleString()}
                . The document was destroyed at that point — its purpose was
                served, and keeping photographs of government IDs is how a
                privacy product becomes the breach.
              </p>
            ) : request.identity_rejection_reason ? (
              <div className="space-y-2">
                <p className="rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-ink">
                  <strong className="font-semibold">Not verified:</strong>{" "}
                  {request.identity_rejection_reason}
                </p>
                <p className="text-xs text-muted">
                  The document was kept, because this decision can be
                  challenged. A new upload reopens the decision.
                </p>
              </div>
            ) : (
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  className="btn-primary"
                  disabled={busy}
                  onClick={() =>
                    guard(
                      () => reviewIdentity(requestId, { accept: true }),
                      "Identity verified. The document has been destroyed.",
                    )
                  }
                >
                  This is them
                </button>
                <input
                  className="input max-w-sm"
                  placeholder="Why not? (required to refuse)"
                  value={refusal}
                  onChange={(e) => setRefusal(e.target.value)}
                />
                <button
                  type="button"
                  className="btn-danger"
                  disabled={busy || !refusal.trim()}
                  onClick={() =>
                    guard(
                      () =>
                        reviewIdentity(requestId, {
                          accept: false,
                          reason: refusal.trim(),
                        }),
                      "Recorded as not verified.",
                    )
                  }
                >
                  Refuse
                </button>
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-3">
            <p className="text-sm text-muted">
              No identity document has been submitted. The person can upload one
              from their own account, or you can attach what they sent you by
              another route.
            </p>
            <label className="btn-secondary inline-flex cursor-pointer items-center">
              Attach a document
              <input
                type="file"
                className="hidden"
                accept="image/png,image/jpeg,image/webp,image/heic,application/pdf"
                onChange={async (e) => {
                  const file = e.target.files?.[0];
                  if (!file) return;
                  await guard(
                    () => uploadIdentityDocument(requestId, file),
                    "Document attached.",
                  );
                  e.target.value = "";
                }}
              />
            </label>
            {request.verified_at && (
              <p className="text-xs text-muted">
                This request already records verification by{" "}
                {request.verification_method || "another route"} on{" "}
                {new Date(request.verified_at).toLocaleDateString()}. A document
                is only needed if that is not enough.
              </p>
            )}
          </div>
        )}
      </Section>

      {/* ------------------------------------------------------- messages -- */}
      <Section
        step="2"
        title="Talk to them"
        subtitle="Kept with the request, so how it was handled is on one record rather than in somebody's inbox."
        done={false}
      >
        <div className="h-[420px]">
          <MessageThread
            requestId={requestId}
            side="staff"
            readOnly={closed}
            onError={(e) => setError(e.message)}
          />
        </div>
      </Section>

      {/* ----------------------------------------------------- disclosure -- */}
      {isAccess ? (
        <Section
          step="3"
          title="What would be disclosed"
          subtitle="Field names, locations, and the values that identify nobody on their own."
          done={Boolean(request.package_delivered_at)}
        >
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                className="btn-secondary"
                disabled={previewBusy}
                onClick={async () => {
                  setPreviewBusy(true);
                  setError("");
                  try {
                    setPreview(await previewDisclosure(requestId));
                  } catch (e) {
                    setError(e.message);
                  } finally {
                    setPreviewBusy(false);
                  }
                }}
              >
                {previewBusy ? "Checking…" : "Check what we hold"}
              </button>
              {preview && (
                <p className="text-sm text-muted">
                  {preview.field_count} field(s) across{" "}
                  {preview.collections.length} source(s) ·{" "}
                  {preview.counts.sensitive} excluded from your view ·{" "}
                  {preview.counts.never} withheld from everyone
                </p>
              )}
            </div>

            {preview && preview.field_count > 0 && (
              <>
                <p className="rounded-lg border border-info/40 bg-info/10 px-3 py-2 text-sm text-ink">
                  Values marked{" "}
                  <span className="font-mono text-xs">{EXCLUDED_MARKER}</span>{" "}
                  are not sent to this screen at all. You can see{" "}
                  <em>that</em> we hold the field, which is what checking the
                  disclosure needs — the person receives the real value.
                </p>

                {/* Wide table, its own scroll container. */}
                <div className="overflow-x-auto rounded-lg border border-line">
                  <table className="min-w-full text-sm">
                    <thead className="bg-canvas text-left text-xs uppercase tracking-wide text-muted">
                      <tr>
                        <th className="px-3 py-2">Source</th>
                        <th className="px-3 py-2">Record</th>
                        <th className="px-3 py-2">Field</th>
                        <th className="px-3 py-2">Value</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-line">
                      {preview.rows.map((r, i) => (
                        <tr key={`${r.collection}-${r.record}-${r.field}-${i}`}>
                          <td className="px-3 py-2 font-mono text-xs text-muted">
                            {r.collection}
                          </td>
                          <td className="px-3 py-2 text-muted">{r.record || "—"}</td>
                          <td className="px-3 py-2 font-mono text-xs">{r.field}</td>
                          <td className="px-3 py-2">
                            <ValueCell
                              value={r.value}
                              classification={r.classification}
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}

            {preview && preview.field_count === 0 && (
              <p className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-ink">
                Nothing was found for this person. That may be the correct
                answer — say so in a message and complete the request. Do not
                send an empty file: check first that the connections are working
                and that the request found the right identifiers, because an
                unreachable system looks exactly like this.
              </p>
            )}

            {/* ------------------------------------------ assemble / send -- */}
            <div className="space-y-3 rounded-lg border border-line bg-canvas p-4">
              <h3 className="font-semibold text-ink">Assemble and send</h3>

              {request.package_assembled_at ? (
                <p className="text-sm text-ink">
                  A package was assembled{" "}
                  {new Date(request.package_assembled_at).toLocaleString()}
                  {assembled && <> · {humanSize(assembled.package.byte_size)}</>}
                  {assembled && (
                    <>
                      {" "}
                      · sha256{" "}
                      <span className="font-mono text-xs">
                        {assembled.package.sha256.slice(0, 16)}…
                      </span>
                    </>
                  )}
                  .{" "}
                  {request.package_delivered_at ? (
                    <strong className="font-semibold text-success">
                      Delivered{" "}
                      {new Date(request.package_delivered_at).toLocaleString()}.
                    </strong>
                  ) : (
                    <strong className="font-semibold text-warning">
                      Not yet sent.
                    </strong>
                  )}
                </p>
              ) : (
                <p className="text-sm text-muted">
                  Nothing assembled yet. This gathers this person's entire
                  record into one file, so the reference has to be typed back.
                </p>
              )}

              <div className="flex flex-wrap items-end gap-2">
                <div>
                  <label className="label" htmlFor="confirm-ref">
                    Type {request.reference} to confirm
                  </label>
                  <input
                    id="confirm-ref"
                    className="input max-w-xs"
                    value={confirm}
                    onChange={(e) => setConfirm(e.target.value)}
                    placeholder={request.reference}
                  />
                </div>
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={
                    busy ||
                    confirm.trim().toUpperCase() !==
                      request.reference.toUpperCase()
                  }
                  onClick={async () => {
                    const result = await guard(
                      () => assembleDisclosure(requestId, confirm.trim()),
                      "Package assembled. Nothing has been sent yet.",
                    );
                    if (result) {
                      setAssembled(result);
                      setConfirm("");
                    }
                  }}
                >
                  {request.package_assembled_at ? "Re-assemble" : "Assemble"}
                </button>
              </div>

              {request.package_assembled_at && (
                <div className="space-y-2 border-t border-line pt-3">
                  <label className="label" htmlFor="covering-note">
                    Covering message (optional)
                  </label>
                  <textarea
                    id="covering-note"
                    className="input min-h-[70px]"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="Left blank, we send a plain note saying the information is attached and when it expires."
                  />
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="text-xs text-muted">
                      The file is never emailed. They are told it is waiting and
                      collect it after signing in.
                    </p>
                    <button
                      type="button"
                      className="btn-primary"
                      disabled={busy}
                      onClick={() =>
                        guard(
                          () => deliverDisclosure(requestId, note),
                          "Sent. They have been notified.",
                        )
                      }
                    >
                      {request.package_delivered_at ? "Send again" : "Send it"}
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        </Section>
      ) : (
        <Section
          step="3"
          title="Carrying it out"
          subtitle={`A ${request.type} request is not answered with a disclosure package.`}
          done={closed}
        >
          <p className="text-sm text-muted">
            {request.type === "erasure" ? (
              <>
                Erasure happens on the data map, where you can see which systems
                hold this person and choose what must be retained.{" "}
                <Link
                  to={`/admin/dsar/${requestId}/data-map`}
                  className="text-teal underline"
                >
                  Open the data map →
                </Link>
              </>
            ) : (
              <>
                Correction is a manual workflow — the engine has no correction
                action. Make the change in the source system, then tell the
                person what you changed using the conversation above and complete
                the request.
              </>
            )}
          </p>
          {request.correction_payload && (
            <pre className="mt-3 overflow-x-auto rounded-lg border border-line bg-canvas p-3 text-xs">
              {JSON.stringify(request.correction_payload, null, 2)}
            </pre>
          )}
        </Section>
      )}
    </div>
  );
}
