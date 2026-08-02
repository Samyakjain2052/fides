// ============================================================================
// Preference Centre (/user/preferences)
// Where a user manages consents they already gave. Filter tabs, one card per
// consent, withdraw behind a ConfirmModal that spells out the consequences.
// ============================================================================
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getConsents, getNotices, updateConsent } from "../../api";
import { useApp } from "../../context/AppContext";
import ConsentCard from "../../components/common/ConsentCard";
import ConfirmModal from "../../components/common/ConfirmModal";

const TABS = [
  { id: "all", label: "All" },
  { id: "active", label: "Active" },
  { id: "withdrawn", label: "Withdrawn" },
  { id: "expiring", label: "Expiring Soon" },
];

const DAY = 864e5;

export default function PreferenceCentre() {
  const { t, notify } = useApp();
  const navigate = useNavigate();
  const [notices, setNotices] = useState([]);
  const [consents, setConsents] = useState([]);
  const [tab, setTab] = useState("all");
  const [pending, setPending] = useState(null); // the consent awaiting confirmation
  const [busy, setBusy] = useState(false);
  const [lastAudit, setLastAudit] = useState(null);

  const load = () => Promise.all([getNotices(), getConsents()]).then(([n, c]) => {
    setNotices(n);
    setConsents(c);
  });

  useEffect(() => {
    load();
  }, []);

  const rows = useMemo(
    () =>
      consents.map((c) => {
        const notice = notices.find((n) => n.id === c.notice_id);
        const days = c.expires_at ? Math.ceil((new Date(c.expires_at) - Date.now()) / DAY) : null;
        return { consent: c, notice, days };
      }).filter((r) => r.notice),
    [consents, notices]
  );

  const filtered = rows.filter(({ consent, days }) => {
    if (tab === "active") return consent.status === "active";
    if (tab === "withdrawn") return consent.status === "withdrawn";
    if (tab === "expiring") return consent.status === "active" && days !== null && days < 30;
    return true;
  });

  const requestChange = (row, nextValue) => {
    // Turning a consent ON is not destructive — do it immediately.
    if (nextValue) {
      apply(row.consent.id, "active");
      return;
    }
    // Turning it OFF is destructive — confirm, with consequences.
    setPending(row);
  };

  const apply = async (consentId, status) => {
    setBusy(true);
    try {
      const res = await updateConsent(consentId, status);
      setLastAudit(res.audit);
      await load();
      notify(
        status === "withdrawn"
          ? "Consent withdrawn. An audit entry has been recorded."
          : "Consent re-given. An audit entry has been recorded."
      );
    } finally {
      setBusy(false);
      setPending(null);
    }
  };

  const downloadAll = () => {
    // Client-side CSV so the button does something real without a backend.
    const header = "reference,purpose,status,given_at,expires_at,withdrawn_at,language,version,method";
    const lines = consents.map((c) =>
      [c.id, c.purpose, c.status, c.given_at, c.expires_at || "", c.withdrawn_at || "", c.language, c.version, c.method].join(",")
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
              : rows.filter(({ consent, days }) =>
                  tb.id === "expiring"
                    ? consent.status === "active" && days !== null && days < 30
                    : consent.status === tb.id
                ).length;
          return (
            <button
              key={tb.id}
              type="button"
              onClick={() => setTab(tb.id)}
              className={`rounded-full px-3.5 py-1.5 text-sm transition ${
                tab === tb.id ? "bg-navy text-white" : "bg-surface text-ink border border-line hover:bg-line/40"
              }`}
            >
              {t(tb.label)} <span className="opacity-70">({count})</span>
            </button>
          );
        })}
      </div>

      {lastAudit && (
        <div className="card border-navy/20 bg-navy/5 p-3 text-xs text-ink">
          Audit entry <strong>{lastAudit.log_id}</strong> created at{" "}
          {new Date(lastAudit.timestamp).toLocaleString()} — action{" "}
          <strong>{lastAudit.action_type}</strong>, hash{" "}
          <span className="font-mono">{lastAudit.audit_hash.slice(0, 22)}…</span>
        </div>
      )}

      <div className="space-y-4">
        {filtered.length === 0 && (
          <p className="card p-6 text-center text-sm text-muted">
            Nothing in this view.
          </p>
        )}
        {filtered.map(({ consent, notice, days }) => (
          <ConsentCard
            key={consent.id}
            notice={notice}
            consent={consent}
            variant="preference"
            daysToExpiry={days}
            checked={consent.status === "active"}
            onChange={(value) => requestChange({ consent, notice }, value)}
            onHistory={() => navigate("/user/consent-history")}
          />
        ))}
      </div>

      <ConfirmModal
        open={Boolean(pending)}
        title={`Withdraw consent for ${pending?.notice?.purpose || ""}?`}
        body="You can give this consent again later, but the change takes effect immediately."
        consequences={[
          pending?.notice?.withdrawal_policy,
          `Processing for “${pending?.notice?.purpose}” stops.`,
          "The withdrawal is timestamped and written to the audit trail.",
          "Data already collected is kept only as long as the retention policy allows.",
        ].filter(Boolean)}
        confirmLabel="Yes, withdraw consent"
        busy={busy}
        onCancel={() => setPending(null)}
        onConfirm={() => apply(pending.consent.id, "withdrawn")}
      />
    </div>
  );
}
