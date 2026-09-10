// ============================================================================
// Vendors (/admin/vendors)
//
// The processors and third parties that hold this company's personal data.
// §8(2) keeps the Data Fiduciary responsible for their processing, which makes
// this register part of the company's own compliance surface rather than a
// procurement nicety.
//
// NO SCORE. There is no number per vendor anywhere on this screen, and its
// absence is the design. A composite score is the output of a research
// operation — litigation feeds, breach databases, diffed policies — and
// deriving one from what somebody typed into this form would invent an
// authority we do not have. What is shown instead is a list of specific
// concerns, each with what turns on it, because "no signed DPA" is something a
// person can act on this afternoon and "68/100" is not.
//
// The screen leads with the two facts that matter most and are most often
// missing: whether there is a signed agreement, and whether anybody knows how
// to get one person deleted from this vendor's systems.
// ============================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CERTIFICATIONS,
  DATA_CATEGORIES,
  DOCUMENT_KINDS,
  ROLE_LABEL,
  STATUS_LABEL,
  STATUS_TONE,
  TIER_LABEL,
  addDocument,
  createVendor,
  decideVendor,
  linkSystem,
  updateVendor,
  vendors as loadVendors,
  vendor as loadVendor,
} from "../../api/vendors";
import { listUsers } from "../../api/auth";
import { listConnections } from "../../api/connections";
import { useApp } from "../../context/AppContext";

const TONE_DOT = {
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
  info: "bg-info",
  neutral: "bg-muted",
};

const SEVERITY_STYLE = {
  high: "border-danger/40 bg-danger/5",
  medium: "border-warning/40 bg-warning/10",
  low: "border-line bg-canvas",
};

function Badge({ tone, children }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-line bg-surface px-2 py-0.5 text-xs text-ink">
      <span
        className={`h-2 w-2 shrink-0 rounded-full ${TONE_DOT[tone] || "bg-muted"}`}
        aria-hidden="true"
      />
      {children}
    </span>
  );
}

/** The concerns list. Each item says what turns on it. */
function Concerns({ items }) {
  if (!items || items.length === 0) {
    return (
      <p className="text-sm text-success">
        Nothing outstanding on the register for this vendor.
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {items.map((c, i) => (
        <li
          key={i}
          className={`rounded-lg border px-3 py-2 ${SEVERITY_STYLE[c.severity]}`}
        >
          <p className="text-sm font-medium text-ink">{c.title}</p>
          <p className="mt-0.5 text-xs text-muted">{c.why}</p>
        </li>
      ))}
    </ul>
  );
}

export default function Vendors() {
  const { notify } = useApp();

  const [data, setData] = useState(null);
  const [detail, setDetail] = useState(null);
  const [people, setPeople] = useState([]);
  const [connections, setConnections] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState("all");

  const [form, setForm] = useState({
    name: "",
    domain: "",
    role: "processor",
    riskTier: "medium",
    ownerUserId: "",
  });
  const [doc, setDoc] = useState({ kind: "privacy_policy", title: "", url: "" });

  const load = useCallback(async () => {
    try {
      setData(await loadVendors());
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    listUsers()
      .then((u) => setPeople(u.filter((p) => p.is_active)))
      .catch(() => setPeople([]));
    listConnections()
      .then(setConnections)
      .catch(() => setConnections([]));
  }, []);

  const open = async (id) => {
    if (detail?.id === id) {
      setDetail(null);
      return;
    }
    try {
      setDetail(await loadVendor(id));
    } catch (e) {
      setError(e.message);
    }
  };

  const run = async (fn, ok) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      if (ok) notify(ok);
      await load();
      if (detail) setDetail(await loadVendor(detail.id));
      return true;
    } catch (e) {
      setError(e.message);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const shown = useMemo(() => {
    const items = data?.items || [];
    if (filter === "all") return items;
    if (filter === "in_use") return items.filter((v) => v.in_use);
    if (filter === "no_dpa") return items.filter((v) => v.dpa_missing);
    if (filter === "concerns")
      return items.filter((v) => v.worst_severity === "high");
    if (filter === "review") return items.filter((v) => v.review_overdue);
    return items.filter((v) => v.status === filter);
  }, [data, filter]);

  if (!data) return <p className="text-sm text-muted">Loading the register…</p>;

  const { counts } = data;

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">Vendors</h1>
          <p className="max-w-2xl text-sm text-muted">
            The processors and third parties that hold your data. §8(2) keeps you
            responsible for their processing, which is why this is a compliance
            register and not a supplier list.
          </p>
        </div>
        <button
          type="button"
          className="btn-primary"
          onClick={() => setCreating((v) => !v)}
        >
          {creating ? "Cancel" : "Add a vendor"}
        </button>
      </header>

      {/* --------------------------------------------------- headline facts -- */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[
          ["in_use", counts.in_use, "in use", "neutral"],
          ["no_dpa", counts.no_dpa, "with no signed agreement", "danger"],
          ["concerns", counts.with_high_concerns, "with a serious gap", "danger"],
          ["review", counts.review_overdue, "overdue for review", "warning"],
        ].map(([id, value, label, tone]) => (
          <button
            key={id}
            type="button"
            onClick={() => setFilter(id)}
            className={`card p-4 text-left ${
              value > 0 && tone === "danger" ? "border-danger/40" : ""
            }`}
          >
            <p className="text-2xl font-semibold text-ink">{value}</p>
            <p className="text-sm text-muted">{label}</p>
          </button>
        ))}
      </div>

      {/* No score, and the screen says why rather than leaving a gap where a
          buyer expects one. */}
      <p className="rounded-lg border border-info/40 bg-info/10 px-4 py-3 text-sm text-ink">
        There is deliberately no risk score here. A number per vendor is the
        output of a research operation — court filings, breach databases,
        continuously diffed policies — and generating one from a form would be
        inventing an authority we do not have. What you get instead is a list of
        specific things to fix, each with what turns on it.
      </p>

      {error && (
        <p className="rounded-lg border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">
          {error}
        </p>
      )}

      {/* --------------------------------------------------------- create -- */}
      {creating && (
        <form
          className="card grid gap-4 p-5 sm:grid-cols-2"
          onSubmit={async (e) => {
            e.preventDefault();
            const ok = await run(
              () =>
                createVendor({
                  name: form.name.trim(),
                  domain: form.domain.trim(),
                  role: form.role,
                  riskTier: form.riskTier,
                  ownerUserId: form.ownerUserId || null,
                }),
              "Added, as under review. Approving them is a separate decision.",
            );
            if (ok) {
              setCreating(false);
              setForm({
                name: "",
                domain: "",
                role: "processor",
                riskTier: "medium",
                ownerUserId: "",
              });
            }
          }}
        >
          <div>
            <label className="label" htmlFor="v-name">
              Name
            </label>
            <input
              id="v-name"
              className="input"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              required
            />
          </div>
          <div>
            <label className="label" htmlFor="v-domain">
              Domain
            </label>
            <input
              id="v-domain"
              className="input"
              value={form.domain}
              onChange={(e) => setForm({ ...form, domain: e.target.value })}
              placeholder="mailchimp.com"
            />
          </div>
          <div>
            <label className="label" htmlFor="v-role">
              How do they relate to the data?
            </label>
            <select
              id="v-role"
              className="input"
              value={form.role}
              onChange={(e) => setForm({ ...form, role: e.target.value })}
            >
              {Object.entries(ROLE_LABEL).map(([id, label]) => (
                <option key={id} value={id}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="v-tier">
              How much would a failure here matter?
            </label>
            <select
              id="v-tier"
              className="input"
              value={form.riskTier}
              onChange={(e) => setForm({ ...form, riskTier: e.target.value })}
            >
              {Object.entries(TIER_LABEL).map(([id, label]) => (
                <option key={id} value={id}>
                  {label}
                </option>
              ))}
            </select>
            <p className="mt-1 text-xs text-muted">
              Your judgement, not a computed one — it depends on what you use
              them for.
            </p>
          </div>
          <div>
            <label className="label" htmlFor="v-owner">
              Who owns this relationship
            </label>
            <select
              id="v-owner"
              className="input"
              value={form.ownerUserId}
              onChange={(e) =>
                setForm({ ...form, ownerUserId: e.target.value })
              }
            >
              <option value="">Nobody yet</option>
              {people.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.full_name || p.email}
                </option>
              ))}
            </select>
          </div>
          <div className="sm:col-span-2">
            <button
              type="submit"
              className="btn-primary"
              disabled={busy || !form.name.trim()}
            >
              Add
            </button>
          </div>
        </form>
      )}

      {/* -------------------------------------------------------- filters -- */}
      <div className="flex flex-wrap gap-2">
        {[
          ["all", "All"],
          ["in_use", "In use"],
          ["prospective", "Under review"],
          ["no_dpa", "No agreement"],
          ["concerns", "Serious gaps"],
          ["retired", "Retired"],
        ].map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => setFilter(id)}
            className={`rounded-full border px-3 py-1 text-sm ${
              filter === id
                ? "border-navy bg-navy text-white"
                : "border-line bg-surface text-muted hover:border-navy/40"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* ----------------------------------------------------------- list -- */}
      {shown.length === 0 ? (
        <div className="card p-6 text-center">
          <p className="text-sm text-muted">
            {(data.items || []).length === 0
              ? "No vendors recorded. Every company that holds your customers' data on your behalf belongs here — including the ones this product has no connection to."
              : "Nothing matches that filter."}
          </p>
        </div>
      ) : (
        <ul className="space-y-3">
          {shown.map((v) => (
            <li key={v.id} className="card overflow-hidden">
              <div className="flex flex-wrap items-start justify-between gap-3 p-4">
                <div className="min-w-0">
                  <button
                    type="button"
                    className="font-medium text-ink hover:underline"
                    onClick={() => open(v.id)}
                  >
                    {v.name}
                  </button>
                  <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                    <Badge tone={STATUS_TONE[v.status]}>
                      {STATUS_LABEL[v.status]}
                    </Badge>
                    <span>{ROLE_LABEL[v.role]}</span>
                    <span>· {TIER_LABEL[v.risk_tier]} impact</span>
                    {v.domain && <span>· {v.domain}</span>}
                    {v.owner_label ? (
                      <span>· {v.owner_label}</span>
                    ) : (
                      v.in_use && <span className="text-warning">· no owner</span>
                    )}
                  </p>
                  {v.decision_note && (
                    <p className="mt-2 max-w-2xl text-sm text-muted">
                      {v.status === "conditional" ? "Outstanding: " : ""}
                      {v.decision_note}
                    </p>
                  )}
                </div>
                <div className="shrink-0 text-right">
                  {v.concern_count > 0 ? (
                    <p
                      className={`text-sm font-medium ${
                        v.worst_severity === "high"
                          ? "text-danger"
                          : "text-warning"
                      }`}
                    >
                      {v.concern_count} thing
                      {v.concern_count === 1 ? "" : "s"} to fix
                    </p>
                  ) : (
                    <p className="text-sm text-success">Nothing outstanding</p>
                  )}
                  <button
                    type="button"
                    className="mt-1 text-xs text-teal underline"
                    onClick={() => open(v.id)}
                  >
                    {detail?.id === v.id ? "Hide" : "Open"}
                  </button>
                </div>
              </div>

              {/* ------------------------------------------------ detail -- */}
              {detail?.id === v.id && (
                <div className="space-y-5 border-t border-line bg-canvas p-4">
                  <div>
                    <h3 className="text-sm font-semibold text-ink">
                      What needs attention
                    </h3>
                    <div className="mt-2">
                      <Concerns items={detail.concerns} />
                    </div>
                  </div>

                  {/* ------------------------------------- the two big ones -- */}
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div className="rounded-lg border border-line bg-surface p-3">
                      <h4 className="text-sm font-semibold text-ink">
                        The agreement
                      </h4>
                      <label className="mt-2 flex items-center gap-2 text-sm">
                        <input
                          type="checkbox"
                          checked={detail.dpa_signed}
                          onChange={(e) => {
                            const signed = e.target.checked;
                            run(
                              () =>
                                updateVendor(detail.id, {
                                  dpa_signed: signed,
                                  // A signed agreement needs its date, and the
                                  // server refuses one without it — default to
                                  // today rather than making somebody find the
                                  // field after an error.
                                  dpa_signed_on: signed
                                    ? detail.dpa_signed_on ||
                                      new Date().toISOString().slice(0, 10)
                                    : null,
                                }),
                              signed
                                ? "Recorded as signed."
                                : "Recorded as unsigned.",
                            );
                          }}
                        />
                        A data processing agreement is signed
                      </label>
                      <div className="mt-2 grid gap-2 sm:grid-cols-2">
                        <div>
                          <label
                            className="label"
                            htmlFor={`signed-${detail.id}`}
                          >
                            Signed on
                          </label>
                          <input
                            id={`signed-${detail.id}`}
                            type="date"
                            className="input py-1 text-sm"
                            defaultValue={detail.dpa_signed_on || ""}
                            onBlur={(e) =>
                              e.target.value !== (detail.dpa_signed_on || "") &&
                              run(() =>
                                updateVendor(detail.id, {
                                  dpa_signed_on: e.target.value || null,
                                }),
                              )
                            }
                          />
                        </div>
                        <div>
                          <label
                            className="label"
                            htmlFor={`expires-${detail.id}`}
                          >
                            Expires
                          </label>
                          <input
                            id={`expires-${detail.id}`}
                            type="date"
                            className="input py-1 text-sm"
                            defaultValue={detail.dpa_expires_on || ""}
                            onBlur={(e) =>
                              e.target.value !==
                                (detail.dpa_expires_on || "") &&
                              run(() =>
                                updateVendor(detail.id, {
                                  dpa_expires_on: e.target.value || null,
                                }),
                              )
                            }
                          />
                        </div>
                      </div>
                    </div>

                    <div className="rounded-lg border border-line bg-surface p-3">
                      <h4 className="text-sm font-semibold text-ink">
                        Getting somebody deleted
                      </h4>
                      <p className="mt-1 text-xs text-muted">
                        The field that turns &ldquo;we use this vendor&rdquo;
                        into &ldquo;here is how somebody gets removed from
                        it&rdquo;.
                      </p>
                      <div className="mt-2 space-y-2">
                        <div>
                          <label
                            className="label"
                            htmlFor={`dsar-${detail.id}`}
                          >
                            Who to ask
                          </label>
                          <input
                            id={`dsar-${detail.id}`}
                            className="input py-1 text-sm"
                            defaultValue={detail.dsar_contact || ""}
                            placeholder="privacy@vendor.example"
                            onBlur={(e) =>
                              e.target.value !== (detail.dsar_contact || "") &&
                              run(() =>
                                updateVendor(detail.id, {
                                  dsar_contact: e.target.value || null,
                                }),
                              )
                            }
                          />
                        </div>
                        <div className="grid gap-2 sm:grid-cols-2">
                          <div>
                            <label
                              className="label"
                              htmlFor={`sla-${detail.id}`}
                            >
                              Their turnaround (days)
                            </label>
                            <input
                              id={`sla-${detail.id}`}
                              type="number"
                              min="1"
                              className="input py-1 text-sm"
                              defaultValue={detail.dsar_sla_days || ""}
                              onBlur={(e) =>
                                run(() =>
                                  updateVendor(detail.id, {
                                    dsar_sla_days: e.target.value
                                      ? Number(e.target.value)
                                      : null,
                                  }),
                                )
                              }
                            />
                          </div>
                          <div>
                            <label
                              className="label"
                              htmlFor={`breach-${detail.id}`}
                            >
                              Breach notice (hours)
                            </label>
                            <input
                              id={`breach-${detail.id}`}
                              type="number"
                              min="1"
                              className="input py-1 text-sm"
                              defaultValue={detail.breach_notice_hours || ""}
                              onBlur={(e) =>
                                run(() =>
                                  updateVendor(detail.id, {
                                    breach_notice_hours: e.target.value
                                      ? Number(e.target.value)
                                      : null,
                                  }),
                                )
                              }
                            />
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>

                  {/* -------------------------------------------- location -- */}
                  <div className="grid gap-4 sm:grid-cols-2">
                    <div>
                      <label className="label" htmlFor={`loc-${detail.id}`}>
                        Where the data is
                      </label>
                      <input
                        id={`loc-${detail.id}`}
                        className="input py-1 text-sm"
                        defaultValue={detail.data_location || ""}
                        placeholder="AWS ap-south-1 (Mumbai), support access from Ireland"
                        onBlur={(e) =>
                          e.target.value !== (detail.data_location || "") &&
                          run(() =>
                            updateVendor(detail.id, {
                              data_location: e.target.value || null,
                            }),
                          )
                        }
                      />
                      <label className="mt-2 flex items-center gap-2 text-sm">
                        <input
                          type="checkbox"
                          checked={detail.transfers_outside_india}
                          onChange={(e) =>
                            run(() =>
                              updateVendor(detail.id, {
                                transfers_outside_india: e.target.checked,
                              }),
                            )
                          }
                        />
                        Data leaves India
                      </label>
                    </div>
                    <div>
                      <p className="label">What they hold</p>
                      <div className="mt-1 flex flex-wrap gap-1">
                        {DATA_CATEGORIES.map((c) => {
                          const on = (detail.data_categories || []).includes(c);
                          return (
                            <button
                              key={c}
                              type="button"
                              onClick={() =>
                                run(() =>
                                  updateVendor(detail.id, {
                                    data_categories: on
                                      ? detail.data_categories.filter(
                                          (x) => x !== c,
                                        )
                                      : [...(detail.data_categories || []), c],
                                  }),
                                )
                              }
                              className={`rounded-full border px-2 py-0.5 text-xs ${
                                on
                                  ? "border-navy bg-navy text-white"
                                  : "border-line bg-surface text-muted"
                              }`}
                            >
                              {c}
                            </button>
                          );
                        })}
                      </div>
                      <p className="mt-2 label">Certifications claimed</p>
                      <div className="mt-1 flex flex-wrap gap-1">
                        {CERTIFICATIONS.map((c) => {
                          const on = (detail.certifications || []).includes(c);
                          return (
                            <button
                              key={c}
                              type="button"
                              onClick={() =>
                                run(() =>
                                  updateVendor(detail.id, {
                                    certifications: on
                                      ? detail.certifications.filter(
                                          (x) => x !== c,
                                        )
                                      : [...(detail.certifications || []), c],
                                  }),
                                )
                              }
                              className={`rounded-full border px-2 py-0.5 text-xs ${
                                on
                                  ? "border-navy bg-navy text-white"
                                  : "border-line bg-surface text-muted"
                              }`}
                            >
                              {c}
                            </button>
                          );
                        })}
                      </div>
                      <p className="mt-1 text-xs text-muted">
                        Claimed, not verified — attach the certificate below to
                        evidence it.
                      </p>
                    </div>
                  </div>

                  {/* --------------------------------------------- systems -- */}
                  <div>
                    <h4 className="text-sm font-semibold text-ink">
                      Systems they supply
                    </h4>
                    <p className="mt-1 text-xs text-muted">
                      Links this vendor to the data map, so &ldquo;this vendor is
                      retired — which of our systems does that affect?&rdquo; has
                      an answer.
                    </p>
                    <ul className="mt-2 flex flex-wrap gap-2">
                      {(detail.systems || []).map((s) => (
                        <li
                          key={s.id}
                          className="rounded-full border border-line bg-surface px-2 py-0.5 text-xs text-ink"
                        >
                          {s.label}
                        </li>
                      ))}
                      {(detail.systems || []).length === 0 && (
                        <li className="text-xs text-muted">None linked.</li>
                      )}
                    </ul>
                    {connections.length > 0 && (
                      <select
                        className="input mt-2 max-w-xs py-1 text-sm"
                        value=""
                        onChange={(e) =>
                          e.target.value &&
                          run(
                            () => linkSystem(detail.id, e.target.value),
                            "Linked.",
                          )
                        }
                      >
                        <option value="">Link a connected system…</option>
                        {connections
                          .filter(
                            (c) =>
                              !(detail.systems || []).some(
                                (s) => s.id === c.id,
                              ),
                          )
                          .map((c) => (
                            <option key={c.id} value={c.id}>
                              {c.label} ({c.connector_label})
                            </option>
                          ))}
                      </select>
                    )}
                  </div>

                  {/* ------------------------------------------- documents -- */}
                  <div>
                    <h4 className="text-sm font-semibold text-ink">Documents</h4>
                    <ul className="mt-2 space-y-1">
                      {(detail.documents || []).map((d) => (
                        <li
                          key={d.id}
                          className="flex flex-wrap items-center gap-2 rounded border border-line bg-surface px-2 py-1.5 text-xs"
                        >
                          <span className="text-muted">
                            {DOCUMENT_KINDS.find(([k]) => k === d.kind)?.[1] ||
                              d.kind}
                          </span>
                          {d.url ? (
                            <a
                              href={d.url}
                              target="_blank"
                              rel="noreferrer noopener"
                              className="flex-1 truncate text-teal underline"
                            >
                              {d.title}
                            </a>
                          ) : (
                            <span className="flex-1 truncate text-ink">
                              {d.title}
                            </span>
                          )}
                          {d.changed_since_review && (
                            <span className="rounded-full border border-warning/50 bg-warning/10 px-2 py-0.5 text-ink">
                              changed since last read
                            </span>
                          )}
                          {d.last_seen_at && (
                            <span className="text-muted">
                              read{" "}
                              {new Date(d.last_seen_at).toLocaleDateString()}
                            </span>
                          )}
                        </li>
                      ))}
                      {(detail.documents || []).length === 0 && (
                        <li className="text-xs text-muted">
                          Nothing recorded. Their privacy policy and
                          sub-processor list are the two worth linking first.
                        </li>
                      )}
                    </ul>

                    <div className="mt-2 flex flex-wrap items-end gap-2">
                      <div>
                        <label className="label" htmlFor={`dk-${detail.id}`}>
                          Kind
                        </label>
                        <select
                          id={`dk-${detail.id}`}
                          className="input py-1 text-sm"
                          value={doc.kind}
                          onChange={(e) =>
                            setDoc({ ...doc, kind: e.target.value })
                          }
                        >
                          {DOCUMENT_KINDS.map(([k, label]) => (
                            <option key={k} value={k}>
                              {label}
                            </option>
                          ))}
                        </select>
                      </div>
                      <div className="min-w-[150px]">
                        <label className="label" htmlFor={`dt-${detail.id}`}>
                          Title
                        </label>
                        <input
                          id={`dt-${detail.id}`}
                          className="input py-1 text-sm"
                          value={doc.title}
                          onChange={(e) =>
                            setDoc({ ...doc, title: e.target.value })
                          }
                        />
                      </div>
                      <div className="min-w-[220px] flex-1">
                        <label className="label" htmlFor={`du-${detail.id}`}>
                          Link
                        </label>
                        <input
                          id={`du-${detail.id}`}
                          className="input py-1 text-sm"
                          value={doc.url}
                          onChange={(e) =>
                            setDoc({ ...doc, url: e.target.value })
                          }
                          placeholder="https://vendor.example/privacy"
                        />
                      </div>
                      <button
                        type="button"
                        className="btn-secondary text-sm"
                        disabled={busy || !doc.title.trim() || !doc.url.trim()}
                        onClick={async () => {
                          const ok = await run(
                            () =>
                              addDocument(detail.id, {
                                kind: doc.kind,
                                title: doc.title.trim(),
                                url: doc.url.trim(),
                              }),
                            "Recorded.",
                          );
                          if (ok)
                            setDoc({ kind: doc.kind, title: "", url: "" });
                        }}
                      >
                        Record
                      </button>
                    </div>
                  </div>

                  {/* -------------------------------------------- decision -- */}
                  <div className="border-t border-line pt-3">
                    <h4 className="text-sm font-semibold text-ink">Decision</h4>
                    <p className="mt-1 text-xs text-muted">
                      Whether this vendor may receive personal data. Refusing
                      needs a reason, and so does approving with conditions —
                      the note is the record of what remains outstanding.
                    </p>
                    <div className="mt-2 flex flex-wrap gap-2">
                      <button
                        type="button"
                        className="btn-primary text-sm"
                        disabled={busy}
                        onClick={() =>
                          run(
                            () =>
                              decideVendor(detail.id, { status: "approved" }),
                            "Approved.",
                          )
                        }
                      >
                        Approve
                      </button>
                      <button
                        type="button"
                        className="btn-secondary text-sm"
                        disabled={busy}
                        onClick={() => {
                          const note = window.prompt(
                            "What remains outstanding? This becomes the record of the conditions.",
                          );
                          if (note?.trim())
                            run(
                              () =>
                                decideVendor(detail.id, {
                                  status: "conditional",
                                  note: note.trim(),
                                }),
                              "Approved with conditions.",
                            );
                        }}
                      >
                        Approve with conditions
                      </button>
                      <button
                        type="button"
                        className="btn-ghost text-sm text-danger"
                        disabled={busy}
                        onClick={() => {
                          const note = window.prompt(
                            "Why are they being refused? This may be revisited, and somebody will ask.",
                          );
                          if (note?.trim())
                            run(
                              () =>
                                decideVendor(detail.id, {
                                  status: "rejected",
                                  note: note.trim(),
                                }),
                              "Refused.",
                            );
                        }}
                      >
                        Refuse
                      </button>
                      <button
                        type="button"
                        className="btn-ghost text-sm"
                        disabled={busy}
                        onClick={() =>
                          run(
                            () =>
                              decideVendor(detail.id, { status: "retired" }),
                            "Retired. The record is kept.",
                          )
                        }
                      >
                        Retire
                      </button>
                    </div>
                    <div className="mt-3 max-w-xs">
                      <label className="label" htmlFor={`cad-${detail.id}`}>
                        Re-review every (days)
                      </label>
                      <input
                        id={`cad-${detail.id}`}
                        type="number"
                        min="1"
                        className="input py-1 text-sm"
                        defaultValue={detail.review_every_days || ""}
                        placeholder="365"
                        onBlur={(e) =>
                          run(() =>
                            updateVendor(detail.id, {
                              review_every_days: e.target.value
                                ? Number(e.target.value)
                                : null,
                            }),
                          )
                        }
                      />
                    </div>
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
