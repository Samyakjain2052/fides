// ============================================================================
// User Dashboard (/user/dashboard)
// 4 quick-status cards, a recent-activity timeline, 3 quick actions.
//
// Every figure on this screen is now read from the API. It used to call
// `getUserDashboard()`, which filtered three module-level mock arrays, and
// labelled the activity timeline by looking purposes up in `MOCK_NOTICES` — so a
// person was shown invented counts about their own privacy, and the greeting
// called them Priya regardless of who they were.
//
// It also had no entry in `PATH_MODULES`, so the honesty layer gave it no
// preview banner: the one screen with nothing real on it was the one screen not
// declaring itself. The fix is to make it real rather than to label it, since
// every endpoint it needs already existed and is already live elsewhere.
//
// The four sources, all of which other live screens use:
//   preferenceCentre     consents, and how close each is to expiry
//   myRows (dsar)        the person's own rights requests
//   myGrievances         their complaints
//   consentHistoryRows   the activity feed, straight off the audit chain
// ============================================================================
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { consentHistoryRows, preferenceCentre } from "../../api/consent";
import { myRows as myDsarRows } from "../../api/dsar";
import { myGrievances } from "../../api/grievances";
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
  const { t, user } = useApp();
  const [data, setData] = useState(null);
  const [grievances, setGrievances] = useState([]);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!user) return;
    let live = true;

    // Settled, not all-or-nothing. A dashboard is a summary of four independent
    // things; one endpoint failing should cost that card, not the page.
    Promise.allSettled([
      preferenceCentre(user),
      myDsarRows(),
      consentHistoryRows(user),
      myGrievances(),
    ]).then(([prefs, dsar, history, griev]) => {
      if (!live) return;

      const rows = prefs.status === "fulfilled" ? prefs.value.rows : [];
      const active = rows.filter((r) => r.consent?.status === "active");
      const requests = dsar.status === "fulfilled" ? dsar.value : [];

      setData({
        active_consents: active.length,
        pending_dsar: requests.filter(
          (r) => !["completed", "rejected", "cancelled"].includes(r.status),
        ).length,
        // Counted off `daysToExpiry`, which preferenceCentre derives from the
        // consent's own expiry — not from a guess about the retention period.
        // Negative means already lapsed, so it is excluded: that is not
        // "expiring soon", and the Preference Centre reports it separately.
        expiring_soon: active.filter(
          (r) => r.daysToExpiry !== null && r.daysToExpiry >= 0 && r.daysToExpiry <= 30,
        ).length,
        recent_activity:
          history.status === "fulfilled" ? history.value.rows.slice(0, 5) : [],
      });
      setGrievances(griev.status === "fulfilled" ? griev.value : []);

      const failed = [prefs, dsar, history, griev].filter((r) => r.status === "rejected");
      if (failed.length) {
        setError(
          `${failed.length} of 4 sections could not be loaded. What you see below ` +
          "is incomplete rather than zero.",
        );
      }
    });

    return () => {
      live = false;
    };
  }, [user]);

  if (!data) {
    return <p className="text-sm text-muted">Loading your summary…</p>;
  }

  const openGrievances = grievances.filter(
    (g) => !["resolved", "rejected"].includes(g.status),
  ).length;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-ink">
          {t("Welcome")}, {user?.full_name?.split(" ")[0] || "there"}
        </h1>
        <p className="text-sm text-muted">
          Everything we hold about you, and every choice you&apos;ve made, in one place.
        </p>
      </div>

      {/* Said out loud rather than left to look like a zero. A privacy dashboard
          reporting "0 active consents" because a request failed is worse than one
          admitting it could not load. */}
      {error && (
        <div className="rounded-lg border border-warning/50 bg-warning/10 p-3 text-sm text-ink">
          {error}
        </div>
      )}

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
          value={openGrievances}
          tone={openGrievances > 0 ? "warning" : "neutral"}
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
              const isWithdraw = entry.action_type === "withdraw";
              return (
                <li key={entry.log_id} className="flex gap-3">
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
                      {entry.purpose && (
                        <span className="text-muted"> — {entry.purpose}</span>
                      )}
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
