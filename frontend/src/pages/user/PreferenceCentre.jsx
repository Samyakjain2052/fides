// ============================================================================
// Preference Centre (/user/preferences)
//
// Real, as of Phase 3: purposes, published notice versions and consents all come
// from PostgreSQL, and every change writes an entry to the tamper-evident audit
// chain.
//
// One row per **purpose**, not per consent — including purposes never answered.
// A preference centre that only lists existing consents cannot be used to give
// one, which would quietly make "withdraw" the only available action.
// ============================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { grantConsent, preferenceCentre, withdrawConsent } from "../../api/consent";
import { useApp } from "../../context/AppContext";
import ConsentCard from "../../components/common/ConsentCard";
import ConfirmModal from "../../components/common/ConfirmModal";

const TABS = [
  { id: "all", label: "All" },
  { id: "active", label: "Active" },
  { id: "withdrawn", label: "Withdrawn" },
  { id: "expiring", label: "Expiring Soon" },
];

export default function PreferenceCentre() {
  const { t, notify, user } = useApp();
  const navigate = useNavigate();
  const [principal, setPrincipal] = useState(null);
  const [rows, setRows] = useState([]);
  const [tab, setTab] = useState("all");
  const [pending, setPending] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const { principal: p, rows: r } = await preferenceCentre(user);
      setPrincipal(p);
      setRows(r);
      setError(null);
    } catch (e) {
      // Say what failed. A blank screen with no explanation is the version of
      // this that gets reported as "the page is broken".
      setError(e.message || "Could not load your consents.");
    } finally {
      setLoading(false);
    }
  }, [user]);

  useEffect(() => {
    load();
  }, [load]);

  const filtered = useMemo(
    () =>
      rows.filter(({ consent, daysToExpiry }) => {
        if (tab === "active") return consent?.status === "active";
        if (tab === "withdrawn") return consent?.status === "withdrawn";
        if (tab === "expiring")
          return consent?.status === "active" && daysToExpiry !== null && daysToExpiry < 30;
        return true;
      }),
    [rows, tab]
  );

  const requestChange = (row, nextValue) => {
    // Giving consent is not destructive — do it immediately.
    if (nextValue) {
      apply(row, "active");
      return;
    }
    // Withdrawing is — confirm, with the consequences spelled out.
    setPending(row);
  };

  const apply = async (row, status) => {
    setBusy(true);
    try {
      if (status === "withdrawn") {
        await withdrawConsent({ principalId: principal.id, purposeId: row.purpose.id });
        notify("Consent withdrawn. An audit entry has been recorded.");
      } else {
        await grantConsent({
          principalId: principal.id,
          purposeId: row.purpose.id,
          // The version on screen, not "whatever is current when the request
          // lands" — those differ if someone publishes while this page is open.
          noticeId: row.currentNotice?.id,
          method: "checkbox",
          source: "preference-centre",
        });
        notify("Consent recorded. An audit entry has been written.");
      }
      await load();
    } catch (e) {
      // The server refuses some changes for lawful reasons — a mandatory
      // purpose, a purpose with no published notice. Show the reason it gave
      // rather than a generic failure.
      notify(e.message || "That change could not be recorded.", "error");
    } finally {
      setBusy(false);
      setPending(null);
    }
  };

  const downloadAll = () => {
    const header =
      "purpose_key,purpose,status,given_at,expires_at,withdrawn_at,language,notice_version,method";
    const lines = rows
      .filter((r) => r.consent)
      .map((r) =>
        [
          r.purpose.key,
          r.purpose.name,
          r.consent.status,
          r.consent.given_at || "",
          r.consent.expires_at || "",
          r.consent.withdrawn_at || "",
          r.consent.language,
          r.consent.notice_version,
          r.consent.method,
        ].join(",")
      );
    const blob = new Blob([[header, ...lines].join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "my-consents.csv";
    a.click();
    URL.revokeObjectURL(url);
    notify("Your consent record has been downloaded.", "info");
  };

  if (loading) return <p className="text-sm text-muted">Loading your consents…</p>;

  if (error) {
    return (
      <div className="card border-danger/40 bg-danger/5 p-5">
        <p className="font-medium text-ink">Could not load your consents</p>
        <p className="mt-1 text-sm text-muted">{error}</p>
        <button type="button" className="btn-secondary mt-4" onClick={load}>
          Try again
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">{t("My Consent Preferences")}</h1>
          <p className="text-sm text-muted">
            Change any of these at any time. Every change is written to the audit trail.
          </p>
        </div>
        <button type="button" className="btn-secondary" onClick={downloadAll}>
          Download All My Consents
        </button>
      </div>

      <div className="flex flex-wrap gap-2">
        {TABS.map((tb) => {
          const count =
            tb.id === "all"
              ? rows.length
              : rows.filter(({ consent, daysToExpiry }) =>
                  tb.id === "expiring"
                    ? consent?.status === "active" &&
                      daysToExpiry !== null &&
                      daysToExpiry < 30
                    : consent?.status === tb.id
                ).length;
          return (
            <button
              key={tb.id}
              type="button"
              onClick={() => setTab(tb.id)}
              className={`rounded-full px-3.5 py-1.5 text-sm transition ${
                tab === tb.id
                  ? "bg-navy text-white"
                  : "bg-surface text-ink border border-line hover:bg-line/40"
              }`}
            >
              {t(tb.label)} <span className="opacity-70">({count})</span>
            </button>
          );
        })}
      </div>

      <div className="space-y-4">
        {filtered.length === 0 && (
          <p className="card p-6 text-center text-sm text-muted">Nothing in this view.</p>
        )}
        {filtered.map((row) => (
          <div key={row.purpose.id} className="space-y-1">
            <ConsentCard
              notice={{
                purpose: row.purpose.name,
                category: row.purpose.category,
                mandatory: row.purpose.is_mandatory,
                retention_days: row.purpose.retention_days,
                content: row.currentNotice?.content || "",
                data_collected: row.currentNotice?.data_collected || "",
                user_rights: row.currentNotice?.user_rights || "",
                withdrawal_policy: row.currentNotice?.withdrawal_policy || "",
              }}
              consent={
                row.consent || {
                  status: "never_given",
                  given_at: null,
                  version: row.currentNotice?.version,
                  method: "—",
                  language: "English",
                }
              }
              variant="preference"
              daysToExpiry={row.daysToExpiry}
              checked={row.consent?.status === "active"}
              onChange={(value) => requestChange(row, value)}
              onHistory={() => navigate("/user/consent-history")}
            />
            {row.supersededByNewVersion && (
              // Their agreement is still valid; it is just not agreement to the
              // wording now published. Saying so is honest, and it is also what
              // tells a DPO they need to re-collect.
              <p className="px-1 text-xs text-muted">
                You agreed to version {row.agreedNotice.version}. Version{" "}
                {row.currentNotice.version} has since been published — your existing
                consent still stands against the version you read.
              </p>
            )}
          </div>
        ))}
      </div>

      <ConfirmModal
        open={Boolean(pending)}
        title={`Withdraw consent for ${pending?.purpose?.name || ""}?`}
        body="You can give this consent again later, but the change takes effect immediately."
        consequences={[
          pending?.currentNotice?.withdrawal_policy,
          `Processing for “${pending?.purpose?.name}” stops.`,
          "The withdrawal is timestamped and written to the audit trail.",
          "Data already collected is kept only as long as the retention policy allows.",
        ].filter(Boolean)}
        confirmLabel="Yes, withdraw consent"
        busy={busy}
        onCancel={() => setPending(null)}
        onConfirm={() => apply(pending, "withdrawn")}
      />
    </div>
  );
}
