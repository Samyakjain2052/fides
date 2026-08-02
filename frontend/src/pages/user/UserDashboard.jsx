// ============================================================================
// User Dashboard (/user/dashboard)
// 4 quick-status cards, a recent-activity timeline, 3 quick actions.
// ============================================================================
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getUserDashboard, MOCK_NOTICES } from "../../api";
import { useApp } from "../../context/AppContext";
import StatCard from "../../components/common/StatCard";

const ACTION_LABEL = {
  grant: "Consent given",
  withdraw: "Consent withdrawn",
  update: "Consent updated",
  renew: "Consent renewed",
  validate: "Consent validated by the company",
  dsar_submitted: "Data request submitted",
  dsar_completed: "Data request completed",
  dsar_in_progress: "Data request moved to in progress",
  grievance_submitted: "Complaint filed",
  cookie_preferences: "Cookie preferences saved",
  guardian_consent_requested: "Guardian consent requested",
  login: "Signed in",
};

export default function UserDashboard() {
  const { t } = useApp();
  const [data, setData] = useState(null);

  useEffect(() => {
    getUserDashboard().then(setData);
  }, []);

  if (!data) {
    return <p className="text-sm text-muted">Loading your summary…</p>;
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-ink">{t("Welcome")}, Priya</h1>
        <p className="text-sm text-muted">
          Everything we hold about you, and every choice you&apos;ve made, in one place.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label={t("Active Consents")}
          value={data.active_consents}
          tone="success"
          to="/user/preferences"
          hint="Manage in the Preference Centre"
        />
        <StatCard
          label={t("Pending Data Requests")}
          value={data.pending_dsar}
          tone="info"
          to="/user/dsar/status"
          hint="Track progress and deadlines"
        />
        <StatCard
          label={t("Open Grievances")}
          value={data.open_grievances}
          tone={data.open_grievances > 0 ? "warning" : "neutral"}
          to="/user/grievance/status"
          hint="See resolution status"
        />
        <StatCard
          label={t("Expiring Soon")}
          value={data.expiring_soon}
          tone="warning"
          to="/user/preferences"
          hint="Consents lapsing in the next 30 days"
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <section className="card p-5 lg:col-span-2">
          <h2 className="font-semibold text-ink">{t("Recent Activity")}</h2>
          <p className="text-xs text-muted">Your last 5 actions, taken from the audit trail.</p>

          <ol className="mt-4 space-y-0">
            {data.recent_activity.length === 0 && (
              <li className="text-sm text-muted">Nothing recorded yet.</li>
            )}
            {data.recent_activity.map((entry, i, arr) => {
              const notice = MOCK_NOTICES.find((n) => n.id === entry.purpose_id);
              const isWithdraw = entry.action_type === "withdraw";
              return (
                <li key={entry.id} className="flex gap-3">
                  <div className="flex flex-col items-center">
                    <span
                      className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${
                        isWithdraw ? "bg-danger" : "bg-success"
                      }`}
                      aria-hidden="true"
                    />
                    {i < arr.length - 1 && <span className="w-0.5 flex-1 bg-line" aria-hidden="true" />}
                  </div>
                  <div className="pb-5">
                    <p className="text-sm text-ink">
                      {ACTION_LABEL[entry.action_type] || entry.action_type}
                      {notice && <span className="text-muted"> — {notice.purpose}</span>}
                    </p>
                    <p className="text-xs text-muted">
                      {new Date(entry.timestamp).toLocaleString()} · by {entry.initiator}
                    </p>
                  </div>
                </li>
              );
            })}
          </ol>

          <Link to="/user/consent-history" className="text-sm text-teal underline">
            View full consent history
          </Link>
        </section>

        <section className="card p-5">
          <h2 className="font-semibold text-ink">Quick actions</h2>
          <div className="mt-4 space-y-3">
            <Link to="/user/preferences" className="btn-primary w-full">
              {t("Manage My Consents")}
            </Link>
            <Link to="/user/dsar" className="btn-secondary w-full">
              {t("Submit a Data Request")}
            </Link>
            <Link to="/user/grievance" className="btn-secondary w-full">
              {t("File a Complaint")}
            </Link>
          </div>

          <div className="mt-5 rounded-lg border border-line bg-canvas p-3">
            <p className="text-xs font-semibold text-ink">Your rights under the DPDP Act</p>
            <ul className="mt-2 space-y-1 text-xs text-muted">
              <li>• Access a copy of your data</li>
              <li>• Correct anything wrong or incomplete</li>
              <li>• Ask for erasure</li>
              <li>• Withdraw consent at any time</li>
              <li>• Complain, and escalate if unresolved</li>
            </ul>
          </div>
        </section>
      </div>
    </div>
  );
}
