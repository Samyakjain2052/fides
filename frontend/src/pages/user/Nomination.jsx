// ============================================================================
// Nominate somebody (/user/nomination) — DPDP §14
//
// "A Data Principal shall have the right to nominate any other individual, who
// shall, in the event of death or incapacity of the Data Principal, exercise
// the rights of the Data Principal."
//
// A right with no GDPR or US-state equivalent, and the only one that has to be
// exercised BEFORE it is needed — a nomination made after somebody has died is
// not a nomination. That is the whole reason this screen exists and is
// self-service: putting it behind a support request means it is made after the
// event, which is to say never.
//
// The page is written for somebody thinking about their own death, which is
// not a state of mind that tolerates jargon or false cheer. It says what the
// arrangement does, what it does not do, and that nothing happens until it is
// needed — and it does not use the word "beneficiary", because this is not
// about property.
// ============================================================================
import { useCallback, useEffect, useState } from "react";
import {
  SCOPES,
  STATUS_COPY,
  myNomination,
  nominate,
  revokeNomination,
} from "../../api/nomination";
import { useApp } from "../../context/AppContext";

export default function Nomination() {
  const { notify } = useApp();

  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [token, setToken] = useState("");

  const [form, setForm] = useState({
    nomineeName: "",
    nomineeEmail: "",
    nomineePhone: "",
    nomineeRelationship: "",
    scope: "access_only",
    instructions: "",
  });

  const load = useCallback(async () => {
    try {
      setData(await myNomination());
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const created = await nominate(form);
      // Shown once. The server never returns it again, so if the email does
      // not arrive this is the only copy — saying so is better than a token
      // that silently becomes the person's only route and then is lost.
      setToken(created.acceptance_token || "");
      notify(`${form.nomineeName} has been recorded as your nominee.`);
      setShowForm(false);
      setForm({
        nomineeName: "",
        nomineeEmail: "",
        nomineePhone: "",
        nomineeRelationship: "",
        scope: "access_only",
        instructions: "",
      });
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  if (!data) return <p className="text-sm text-muted">Loading…</p>;

  const live = data.live;
  const past = (data.nominations || []).filter((n) => !n.is_live);

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold text-ink">Nominate someone</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted">
          You can name one person to act for you about your data if you die or
          become unable to act for yourself. The law gives you this right —
          section 14 of the Digital Personal Data Protection Act.
        </p>
      </header>

      <div className="card space-y-2 p-5">
        <h2 className="font-semibold text-ink">What this does</h2>
        <ul className="space-y-1 text-sm text-muted">
          <li>
            · Nothing happens now, and nothing changes for you. The arrangement
            sits unused unless it is needed.
          </li>
          <li>
            · If it is needed, somebody here has to see evidence — a death
            certificate, or a court order about your capacity — before your
            nominee can do anything. It is not automatic, and no system decides
            it.
          </li>
          <li>
            · You choose how much they can do. Seeing your data and deleting it
            are separate permissions.
          </li>
          <li>
            · You can change your mind at any time, and you do not have to give
            a reason.
          </li>
          <li>
            · This is only about the data <em>we</em> hold. It is not a will and
            it has nothing to do with your property.
          </li>
        </ul>
      </div>

      {error && (
        <p className="rounded-lg border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">
          {error}
        </p>
      )}

      {token && (
        <div className="rounded-lg border border-info/40 bg-info/10 px-4 py-3 text-sm">
          <p className="font-medium text-ink">
            A confirmation code for your nominee
          </p>
          <p className="mt-1 text-muted">
            We have emailed them about the nomination. If it does not arrive,
            they can use this code to confirm they know about it. We will not be
            able to show it again.
          </p>
          <code className="mt-2 block break-all rounded bg-surface px-2 py-1 font-mono text-xs text-ink">
            {token}
          </code>
          <button
            type="button"
            className="mt-2 text-xs text-teal underline"
            onClick={() => setToken("")}
          >
            I have saved it
          </button>
        </div>
      )}

      {/* --------------------------------------------------------- current -- */}
      {live ? (
        <div className="card p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="font-semibold text-ink">
                {live.nominee_name}
                {live.nominee_relationship && (
                  <span className="ml-2 text-sm font-normal text-muted">
                    ({live.nominee_relationship})
                  </span>
                )}
              </h2>
              <p className="mt-1 text-sm text-muted">{live.nominee_email}</p>
              <p className="mt-2 text-sm text-ink">
                They may {live.scope_words}.
              </p>
              {live.instructions && (
                <p className="mt-2 rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-muted">
                  <span className="text-xs uppercase tracking-wide">
                    Your note
                  </span>
                  <br />
                  {live.instructions}
                </p>
              )}
            </div>
            <div className="text-right">
              <p className="text-sm font-medium text-ink">
                {STATUS_COPY[live.status]?.label || live.status}
              </p>
              <p className="mt-1 max-w-[240px] text-xs text-muted">
                {STATUS_COPY[live.status]?.detail}
              </p>
            </div>
          </div>

          {live.status === "invoked" && (
            <p className="mt-3 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-ink">
              This nomination has been put into effect. If you are reading this
              and did not expect it, contact us immediately — it can be
              reversed, and it should be.
            </p>
          )}

          {live.status !== "invoked" && (
            <div className="mt-4 border-t border-line pt-3">
              <button
                type="button"
                className="btn-ghost text-sm text-danger"
                disabled={busy}
                onClick={async () => {
                  if (
                    !window.confirm(
                      `Withdraw ${live.nominee_name} as your nominee? You do not have to give a reason, and you can nominate somebody else afterwards.`,
                    )
                  )
                    return;
                  setBusy(true);
                  try {
                    await revokeNomination(live.id);
                    notify("Withdrawn.");
                    await load();
                  } catch (e) {
                    setError(e.message);
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                Withdraw this nomination
              </button>
            </div>
          )}
        </div>
      ) : (
        <div className="card p-5">
          <p className="text-sm text-muted">
            You have not nominated anybody. Nothing is wrong with that — but if
            you want to, it has to be done while you are able to, which is the
            whole reason this page exists.
          </p>
          <button
            type="button"
            className="btn-primary mt-3"
            onClick={() => setShowForm((v) => !v)}
          >
            {showForm ? "Cancel" : "Nominate someone"}
          </button>
        </div>
      )}

      {/* ------------------------------------------------------------ form -- */}
      {showForm && (
        <form onSubmit={submit} className="card space-y-4 p-5">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="n-name">
                Their name
              </label>
              <input
                id="n-name"
                className="input"
                value={form.nomineeName}
                onChange={(e) =>
                  setForm({ ...form, nomineeName: e.target.value })
                }
                required
              />
            </div>
            <div>
              <label className="label" htmlFor="n-rel">
                How you know them
              </label>
              <input
                id="n-rel"
                className="input"
                value={form.nomineeRelationship}
                onChange={(e) =>
                  setForm({ ...form, nomineeRelationship: e.target.value })
                }
                placeholder="daughter, brother, solicitor…"
              />
            </div>
            <div>
              <label className="label" htmlFor="n-email">
                Their email
              </label>
              <input
                id="n-email"
                type="email"
                className="input"
                value={form.nomineeEmail}
                onChange={(e) =>
                  setForm({ ...form, nomineeEmail: e.target.value })
                }
                required
              />
              <p className="mt-1 text-xs text-muted">
                We will tell them the nomination exists. A nominee who has never
                heard of it will not use it.
              </p>
            </div>
            <div>
              <label className="label" htmlFor="n-phone">
                Their phone (optional)
              </label>
              <input
                id="n-phone"
                className="input"
                value={form.nomineePhone}
                onChange={(e) =>
                  setForm({ ...form, nomineePhone: e.target.value })
                }
              />
            </div>
          </div>

          <fieldset>
            <legend className="label">What should they be able to do?</legend>
            <div className="mt-1 space-y-2">
              {SCOPES.map((s) => (
                <label
                  key={s.id}
                  className={`flex cursor-pointer gap-3 rounded-lg border p-3 ${
                    form.scope === s.id
                      ? "border-navy/50 bg-navy/5"
                      : "border-line bg-canvas"
                  }`}
                >
                  <input
                    type="radio"
                    name="scope"
                    className="mt-1"
                    checked={form.scope === s.id}
                    onChange={() => setForm({ ...form, scope: s.id })}
                  />
                  <span>
                    <span className="block text-sm font-medium text-ink">
                      {s.label}
                    </span>
                    <span className="mt-0.5 block text-xs text-muted">
                      {s.hint}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          <div>
            <label className="label" htmlFor="n-instructions">
              Anything you want us to know (optional)
            </label>
            <textarea
              id="n-instructions"
              className="input min-h-[80px]"
              value={form.instructions}
              onChange={(e) =>
                setForm({ ...form, instructions: e.target.value })
              }
              placeholder="For example: only after probate, or only the billing records."
            />
            <p className="mt-1 text-xs text-muted">
              A person here will read this. It is not enforced automatically —
              conditions somebody writes are for somebody to read.
            </p>
          </div>

          <button
            type="submit"
            className="btn-primary"
            disabled={
              busy || !form.nomineeName.trim() || !form.nomineeEmail.trim()
            }
          >
            {busy ? "Recording…" : "Record this nomination"}
          </button>
        </form>
      )}

      {/* ------------------------------------------------------- the past -- */}
      {past.length > 0 && (
        <div className="card p-5">
          <h2 className="font-semibold text-ink">Earlier nominations</h2>
          <p className="mt-1 text-xs text-muted">
            Kept so you can see what you have changed.
          </p>
          <ul className="mt-3 divide-y divide-line">
            {past.map((n) => (
              <li
                key={n.id}
                className="flex flex-wrap items-center gap-2 py-2 text-sm"
              >
                <span className="text-ink">{n.nominee_name}</span>
                <span className="text-xs text-muted">
                  {STATUS_COPY[n.status]?.label || n.status}
                  {n.revoked_at &&
                    ` · ${new Date(n.revoked_at).toLocaleDateString()}`}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
