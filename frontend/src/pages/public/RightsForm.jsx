// ============================================================================
// The public rights form (/rights?workspace=…) and its confirmation landing
// page (/confirm-request?workspace=…&token=…).
//
// Both are unauthenticated, and both have to be: a person whose data a company
// holds may have no account with them at all. Requiring one puts a barrier in
// front of a statutory right, and the people most likely to need §11 or §12 —
// somebody whose number was bought from a broker, a former customer, somebody
// asking for erasure precisely because they never signed up — are the least
// likely to have an account.
//
// WHAT THE PAGE PROMISES, IT PROMISES HONESTLY
//
// The deadline comes from the server, from the workspace's own configured SLA.
// A form telling somebody "30 days" while the workspace is set to 15 would be
// worse than one that said nothing. Likewise the page says plainly that the
// request is recorded and the clock starts NOW, and that nothing is looked up
// until the address is confirmed — because both are true and the second reads
// as a delaying tactic unless the first is said alongside it.
//
// This is also what the embeddable snippet renders in an iframe, so it is
// deliberately self-contained: no app chrome, no session, and it works when
// opened cold from an email.
// ============================================================================
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  confirmPublicRequest,
  publicRightsTypes,
  raisePublicRequest,
} from "../../api/publicRights";

function Shell({ title, subtitle, children }) {
  return (
    <div className="min-h-screen bg-canvas px-4 py-10">
      <div className="mx-auto max-w-xl space-y-5">
        <header>
          <h1 className="text-xl font-semibold text-ink">{title}</h1>
          {subtitle && <p className="mt-1 text-sm text-muted">{subtitle}</p>}
        </header>
        {children}
      </div>
    </div>
  );
}

export default function RightsForm() {
  const [params] = useSearchParams();
  const workspace = params.get("workspace") || "";

  const [form, setForm] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(null);

  const [type, setType] = useState("access");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [details, setDetails] = useState({
    field: "",
    current: "",
    corrected: "",
  });

  const load = useCallback(async () => {
    if (!workspace) {
      setError(
        "This form is missing the organisation it belongs to. If you followed " +
          "a link, it may have been cut short — try opening it again.",
      );
      return;
    }
    try {
      setForm(await publicRightsTypes(workspace));
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }, [workspace]);

  useEffect(() => {
    load();
  }, [load]);

  const chosen = form?.types?.find((t) => t.id === type);
  const needsDetails = Boolean(chosen?.needs_details);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      setDone(
        await raisePublicRequest({
          workspace,
          type,
          email: email.trim(),
          name: name.trim() || null,
          details: needsDetails ? details : null,
        }),
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  if (error && !form) {
    return (
      <Shell title="Your data rights">
        <p className="rounded-lg border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">
          {error}
        </p>
      </Shell>
    );
  }

  if (!form) {
    return (
      <Shell title="Your data rights">
        <p className="text-sm text-muted">Loading…</p>
      </Shell>
    );
  }

  if (done) {
    return (
      <Shell title="Your request has been received">
        <div className="card space-y-3 p-6">
          <p className="text-sm text-ink">{done.message}</p>
          <dl className="grid gap-2 border-t border-line pt-3 text-sm">
            <div className="flex justify-between gap-4">
              <dt className="text-muted">Reference</dt>
              <dd className="font-mono text-ink">{done.reference}</dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-muted">They must respond by</dt>
              <dd className="text-ink">
                {new Date(done.deadline_at).toLocaleDateString()}
              </dd>
            </div>
          </dl>
          <p className="text-xs text-muted">
            Keep the reference. If you do not receive the confirmation email,
            check your spam folder before submitting again — a second request
            from the same address is refused while the first is still waiting.
          </p>
        </div>
      </Shell>
    );
  }

  return (
    <Shell
      title={`Your data rights at ${form.organisation}`}
      subtitle={
        `Under the Digital Personal Data Protection Act you can ask to see, ` +
        `correct or erase the personal data ${form.organisation} holds about ` +
        `you. You do not need an account, and you do not have to say why.`
      }
    >
      <form onSubmit={submit} className="card space-y-4 p-6">
        <fieldset>
          <legend className="label">What would you like to do?</legend>
          <div className="mt-1 space-y-2">
            {form.types.map((t) => (
              <label
                key={t.id}
                className={`flex cursor-pointer items-start gap-3 rounded-lg border p-3 ${
                  type === t.id
                    ? "border-navy/50 bg-navy/5"
                    : "border-line bg-canvas"
                }`}
              >
                <input
                  type="radio"
                  name="type"
                  className="mt-1"
                  checked={type === t.id}
                  onChange={() => setType(t.id)}
                />
                <span>
                  <span className="block text-sm font-medium text-ink">
                    {t.label}
                  </span>
                  <span className="mt-0.5 block text-xs text-muted">
                    {t.law}
                  </span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        <div>
          <label className="label" htmlFor="pr-email">
            Your email address
          </label>
          <input
            id="pr-email"
            type="email"
            className="input"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
          <p className="mt-1 text-xs text-muted">
            We will send a link to confirm it is you. Without that,{" "}
            {form.organisation} could be made to disclose or delete somebody
            else&rsquo;s data by anybody who knew their address.
          </p>
        </div>

        <div>
          <label className="label" htmlFor="pr-name">
            Your name (optional)
          </label>
          <input
            id="pr-name"
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>

        {needsDetails && (
          <div className="space-y-3 rounded-lg border border-line bg-canvas p-3">
            <div>
              <label className="label" htmlFor="pr-field">
                {type === "completion"
                  ? "What is missing?"
                  : type === "updating"
                    ? "What has changed?"
                    : "What is wrong?"}
              </label>
              <input
                id="pr-field"
                className="input"
                value={details.field}
                onChange={(e) =>
                  setDetails({ ...details, field: e.target.value })
                }
                placeholder="e.g. my phone number"
                required
              />
            </div>
            <div>
              <label className="label" htmlFor="pr-current">
                {type === "completion"
                  ? "What you have now (if anything)"
                  : "What you have now"}
              </label>
              <input
                id="pr-current"
                className="input"
                value={details.current}
                onChange={(e) =>
                  setDetails({ ...details, current: e.target.value })
                }
              />
            </div>
            <div>
              <label className="label" htmlFor="pr-corrected">
                {type === "updating" ? "The new value" : "What it should be"}
              </label>
              <input
                id="pr-corrected"
                className="input"
                value={details.corrected}
                onChange={(e) =>
                  setDetails({ ...details, corrected: e.target.value })
                }
                required
              />
            </div>
          </div>
        )}

        {error && (
          <p className="rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
            {error}
          </p>
        )}

        <p className="rounded-lg border border-info/40 bg-info/10 px-3 py-2 text-xs text-ink">
          Your request is recorded as soon as you submit it, and{" "}
          {form.organisation} must respond within {form.respond_within_days}{" "}
          days from that moment — confirming your address does not restart that
          clock. But nothing will be looked up or changed until you confirm,
          because acting sooner would mean trusting an email address anybody
          could type here.
        </p>

        <button
          type="submit"
          className="btn-primary w-full"
          disabled={busy || !email.trim()}
        >
          {busy ? "Sending…" : "Submit my request"}
        </button>
      </form>
    </Shell>
  );
}

/** Where the confirmation email lands. */
export function ConfirmRequest() {
  const [params] = useSearchParams();
  const workspace = params.get("workspace") || "";
  const token = params.get("token") || "";

  const [reference, setReference] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // The reference is asked for rather than carried in the link, and that is
  // deliberate. A single-parameter link is a bearer credential in an email; a
  // link plus something the person has to know from their own copy of the
  // acknowledgement means an intercepted URL alone is not enough.
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      setResult(
        await confirmPublicRequest({
          workspace,
          reference: reference.trim(),
          token,
        }),
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  if (result) {
    return (
      <Shell title="Confirmed">
        <p className="rounded-lg border border-success/40 bg-success/5 px-4 py-3 text-sm text-ink">
          {result.message}
        </p>
      </Shell>
    );
  }

  return (
    <Shell
      title="Confirm your data request"
      subtitle="One last step, so we know the request came from you."
    >
      <form onSubmit={submit} className="card space-y-4 p-6">
        {!token && (
          <p className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-ink">
            This link is missing its confirmation code. Email clients sometimes
            cut long links — try opening it again from the email.
          </p>
        )}
        <div>
          <label className="label" htmlFor="cr-reference">
            Your reference
          </label>
          <input
            id="cr-reference"
            className="input font-mono"
            value={reference}
            onChange={(e) => setReference(e.target.value)}
            placeholder="DSAR-2026-0001"
            required
          />
          <p className="mt-1 text-xs text-muted">
            It is in the email, and on the page you saw after submitting.
          </p>
        </div>

        {error && (
          <p className="rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
            {error}
          </p>
        )}

        <button
          type="submit"
          className="btn-primary w-full"
          disabled={busy || !token || !reference.trim()}
        >
          {busy ? "Confirming…" : "Confirm my request"}
        </button>
      </form>
    </Shell>
  );
}
