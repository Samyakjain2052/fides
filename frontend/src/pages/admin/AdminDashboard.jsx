// ============================================================================
// Admin Dashboard (/admin/dashboard)
// The screen a DPO opens every morning: 6 stats, 3 charts, and a
// "needs immediate attention" block that surfaces anything at risk of breaching
// a statutory deadline.
// ============================================================================
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getAdminDashboard } from "../../api";
import { CATEGORY_LABEL, listGrievances, officer as fetchOfficer } from "../../api/grievances";
import { listBreaches } from "../../api/breaches";
import { queueRows } from "../../api/dsar";
import { jobs as fetchJobs } from "../../api/users";
import StatCard from "../../components/common/StatCard";
import StatusBadge from "../../components/common/StatusBadge";
import SLACountdown from "../../components/common/SLACountdown";
import { BarChart, DonutChart } from "../../components/common/Charts";
import { SampleTag } from "../../components/common/PreviewBanner";
import { isPreview } from "../../config/modules";

const DAY = 864e5;

/** An empty chart states that it is empty. It does not invent a shape. */
function EmptyChart({ children }) {
  return (
    <div className="rounded-lg border border-dashed border-line bg-canvas px-4 py-8 text-center">
      <p className="text-sm text-muted">{children}</p>
    </div>
  );
}

export default function AdminDashboard() {
  const [data, setData] = useState(null);
  // Grievances are live now, so they are read from the API rather than taken
  // from the dashboard mock. A real module showing sample numbers on the one
  // screen a DPO opens every morning is the exact failure the honesty layer
  // exists to prevent.
  const [grievances, setGrievances] = useState(null);
  const [officer, setOfficer] = useState(null);
  // Breaches are the loudest thing on this page when they are late. An
  // un-notified breach past its threshold outranks an overdue DSAR: the DSAR is
  // one person's request, the breach is a statutory duty to a regulator and to
  // everybody affected.
  const [breaches, setBreaches] = useState(null);
  // Data requests, from the API. The mock array they used to come from was
  // emptied when the real DSAR record landed and nothing writes to it any more —
  // so these tiles showed a hardcoded 0 while the banner above them claimed the
  // figures were real. Same class of problem as the two live screens that were
  // still reading mocks.
  const [dsar, setDsar] = useState(null);
  // Scheduler health. A stale job means escalation, notification retries or
  // pre-purge warnings have silently stopped — while the rest of this screen
  // implies they are automatic. That inversion is why it is shown here and not on
  // a settings page somebody visits twice a year.
  const [jobs, setJobs] = useState(null);

  useEffect(() => {
    getAdminDashboard().then(setData);
    listGrievances({ overdueOnly: true })
      .then(setGrievances)
      .catch(() => setGrievances(null));
    fetchOfficer().then(setOfficer).catch(() => setOfficer(null));
    listBreaches({ openOnly: true })
      .then(setBreaches)
      .catch(() => setBreaches(null));
    queueRows().then(setDsar).catch(() => setDsar(null));
    // Requires TENANT_MANAGE, so a non-admin simply sees nothing here rather than
    // an error for a permission they are not meant to have.
    fetchJobs().then(setJobs).catch(() => setJobs(null));
  }, []);

  if (!data) return <p className="text-sm text-muted">Loading compliance summary…</p>;

  const { stats, attention, charts } = data;
  const gCounts = grievances?.counts;
  const overdueGrievances = grievances?.items ?? [];

  // Derived from the rows the server returned, so these cannot drift from the
  // queue screen. `overdue` is computed server-side against the clock.
  const dsarRows = dsar?.rows ?? [];
  const openDsar = dsarRows.filter(
    (r) => !["completed", "rejected", "cancelled"].includes(r.status),
  );
  const overdueDsar = openDsar.filter((r) => r.overdue);
  const dsarDueSoon = openDsar.filter((r) => {
    const left = new Date(r.deadline_at) - Date.now();
    return left > 0 && left < 5 * DAY;
  });
  const dsarByType = Object.entries(
    dsarRows.reduce((acc, r) => ({ ...acc, [r.type]: (acc[r.type] || 0) + 1 }), {}),
  ).map(([label, value]) => ({ label, value }));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-ink">Compliance dashboard</h1>
        <p className="text-sm text-muted">
          Consent, rights requests and grievances across the organisation.
        </p>
      </div>

      {/* This screen aggregates several modules at once, so it inherits their
          honesty problem: the DSAR figures are real, the consent and grievance
          figures are sample. Saying so once, at the top, beats tagging each
          tile and hoping the reader assembles the caveat themselves. */}
      <div role="status" className="rounded-lg border border-warning/50 bg-warning/10 px-4 py-3">
        <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
          <span className="rounded-full bg-warning px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-white">
            Mixed
          </span>
          <p className="min-w-0 flex-1 text-sm text-ink">
            <strong className="font-semibold">
              Data request and grievance figures are real.
            </strong>{" "}
            The consent tiles and the consent chart on this page are still sample
            data.{" "}
            <Link to="/roadmap" className="text-teal underline">
              See what is live today
            </Link>
          </p>
        </div>
      </div>

      {/* --------------------------------------------- scheduler is stopped -- */}
      {/* Above the breach alert, because if this is broken the breach alert's own
          escalation is not running either. */}
      {jobs?.jobs?.every((j) => j.stale) && (
        <div role="alert" className="rounded-lg border-2 border-danger bg-danger/10 p-4">
          <p className="font-semibold text-danger">
            The background scheduler is not running
          </p>
          <p className="mt-1 text-sm text-ink">
            Grievance escalation, notification retries and pre-purge warnings are
            not happening — regardless of what the rest of this page implies.
          </p>
          <p className="mt-2 text-xs text-muted">{jobs.note}</p>
        </div>
      )}
      {jobs?.jobs?.some((j) => j.stale) && !jobs.jobs.every((j) => j.stale) && (
        <div className="rounded-lg border border-warning/50 bg-warning/10 p-4 text-sm">
          <p className="font-semibold text-ink">
            {jobs.jobs.filter((j) => j.stale).length} scheduled job
            {jobs.jobs.filter((j) => j.stale).length === 1 ? " has" : "s have"} not
            run recently
          </p>
          <ul className="mt-2 space-y-1 text-xs">
            {jobs.jobs.filter((j) => j.stale).map((j) => (
              <li key={j.job}>
                <span className="font-mono">{j.job}</span> —{" "}
                {j.last_success_at
                  ? `last succeeded ${new Date(j.last_success_at).toLocaleString()}`
                  : "has never succeeded"}
                {j.last_error && <span className="text-danger"> · {j.last_error}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* ------------------------------------------------ breach red alert -- */}
      {/* Above the stats, above everything. A late breach notification is the
          only thing on this screen with a regulator on the other end of it. */}
      {breaches?.items?.some((b) => b.board_overdue) && (
        <div
          role="alert"
          className="rounded-lg border-2 border-danger bg-danger/10 p-4"
        >
          <p className="font-semibold text-danger">
            {breaches.items.filter((b) => b.board_overdue).length} breach
            notification{breaches.items.filter((b) => b.board_overdue).length === 1 ? "" : "s"}{" "}
            overdue to the Data Protection Board
          </p>
          <ul className="mt-2 space-y-1.5 text-sm">
            {breaches.items
              .filter((b) => b.board_overdue)
              .map((b) => (
                <li key={b.id} className="flex flex-wrap items-center gap-3">
                  <span className="font-mono text-xs">{b.reference}</span>
                  <span className="text-ink">{b.title}</span>
                  <span className="text-danger">
                    {Math.round(b.hours_since_discovery)}h since you became aware
                  </span>
                  <Link to="/admin/breaches" className="ml-auto text-teal underline">
                    Open the register
                  </Link>
                </li>
              ))}
          </ul>
          <p className="mt-2 text-xs text-muted">
            {breaches.board_threshold_note}
          </p>
        </div>
      )}
      {/* ---------------------------------------------------------- stats -- */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <StatCard label="Total active consents" value={stats.active_consents} tone="success" sample />
        <StatCard label="Withdrawn this month" value={stats.withdrawn_this_month} tone="neutral" sample />
        <StatCard
          label="Open data requests"
          value={dsar ? openDsar.length : "—"}
          tone="info"
          to="/admin/dsar"
        />
        <StatCard
          label="DSAR overdue"
          value={dsar ? overdueDsar.length : "—"}
          tone={overdueDsar.length > 0 ? "danger" : "neutral"}
          to="/admin/dsar"
          badge={
            overdueDsar.length > 0 ? (
              <span className="rounded-full bg-danger px-2 py-0.5 text-[10px] font-bold text-white">
                ACTION
              </span>
            ) : null
          }
        />
        <StatCard
          label="Open grievances"
          value={gCounts ? gCounts.open : stats.open_grievances}
          tone={gCounts?.overdue ? "danger" : "warning"}
          to="/admin/grievances"
          sample={!gCounts}
          badge={
            gCounts?.overdue ? (
              <span className="rounded-full bg-danger px-2 py-0.5 text-[10px] font-bold text-white">
                {gCounts.overdue} LATE
              </span>
            ) : null
          }
        />
        <StatCard label="Expiring in 30 days" value={stats.expiring_30} tone="warning" sample />
      </div>

      {/* --------------------------------------------------------- charts -- */}
      <div className="grid gap-5 lg:grid-cols-2">
        <section className="card p-5">
          <h2 className="font-semibold text-ink">Data requests by type</h2>
          <p className="text-xs text-muted">All requests on this account</p>
          <div className="mt-4">
            {dsarByType.length ? (
              <BarChart data={dsarByType} />
            ) : (
              <EmptyChart>
                No requests yet. Submit one from the Data Requests screen and it
                will appear here — this chart counts real requests only.
              </EmptyChart>
            )}
          </div>
        </section>

        <section className="card p-5">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="font-semibold text-ink">Consent status distribution</h2>
            {isPreview("consent") && <SampleTag />}
          </div>
          <p className="text-xs text-muted">All consent records</p>
          <div className="mt-4">
            {charts.status_split.length ? (
              <DonutChart data={charts.status_split} />
            ) : (
              <EmptyChart>No consent records yet.</EmptyChart>
            )}
          </div>
        </section>
      </div>

      {/* ------------------------------------------------------ attention -- */}
      <section className="card overflow-hidden">
        <div className="border-b border-line px-5 py-4">
          <h2 className="flex items-center gap-2 font-semibold text-ink">
            <span className="h-2 w-2 rounded-full bg-danger" aria-hidden="true" />
            Needs immediate attention
          </h2>
          <p className="text-xs text-muted">
            Statutory deadlines first. Anything here risks non-compliance.
          </p>
        </div>

        <div className="divide-y divide-line">
          {/* DSARs within 5 days or already overdue */}
          <div className="px-5 py-4">
            <p className="text-sm font-medium text-ink">
              Data requests due within 5 days ({dsarDueSoon.length})
            </p>
            {dsarDueSoon.length === 0 ? (
              <p className="mt-1 text-sm text-muted">Nothing at risk.</p>
            ) : (
              <ul className="mt-2 space-y-2">
                {[...overdueDsar, ...dsarDueSoon].map((r) => {
                  const isOverdue = new Date(r.deadline_at) < Date.now();
                  return (
                    <li
                      key={r.id}
                      className={`flex flex-wrap items-center gap-3 rounded-lg border px-3 py-2 text-sm ${
                        isOverdue ? "border-danger/40 bg-danger/5" : "border-warning/40 bg-warning/5"
                      }`}
                    >
                      <span className="font-mono text-xs">{r.reference}</span>
                      <span className="capitalize text-ink">{r.type}</span>
                      <span className="text-muted">{r.user_email}</span>
                      <StatusBadge status={isOverdue ? "overdue" : r.status} />
                      <span className="ml-auto">
                        <SLACountdown deadlineAt={r.deadline_at} showDate />
                      </span>
                      <Link to="/admin/dsar" className="text-teal underline">Open</Link>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          {/* Grievances past their statutory deadline. Real, from the API —
              `overdueOnly` is evaluated against the clock server-side rather
              than derived here from a constant that would be wrong for any
              customer with a different SLA. */}
          <div className="px-5 py-4">
            <p className="text-sm font-medium text-ink">
              Grievances past their response deadline ({overdueGrievances.length})
            </p>
            {overdueGrievances.length === 0 ? (
              <p className="mt-1 text-sm text-muted">
                {grievances ? "Nothing overdue." : "Could not load grievances."}
              </p>
            ) : (
              <ul className="mt-2 space-y-2">
                {overdueGrievances.map((g) => (
                  <li key={g.id}
                      className="flex flex-wrap items-center gap-3 rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm">
                    <span className="font-mono text-xs">{g.reference}</span>
                    <span className="text-ink">
                      {CATEGORY_LABEL[g.category] || g.category}
                    </span>
                    <span className="text-muted">{g.days_open} days open</span>
                    {g.escalated && <StatusBadge status="escalated" />}
                    <span className="ml-auto">
                      <SLACountdown deadlineAt={g.deadline_at} showDate />
                    </span>
                    <Link to="/admin/grievances" className="text-teal underline">Open</Link>
                  </li>
                ))}
              </ul>
            )}
            {gCounts?.confirmation_expired > 0 && (
              <p className="mt-2 text-xs text-muted">
                {gCounts.confirmation_expired} anonymous complaint
                {gCounts.confirmation_expired === 1 ? "" : "s"} whose confirmation
                window has closed. They will never escalate on their own — if they
                look genuine, somebody has to pick them up.
              </p>
            )}
            {officer && !officer.published && (
              <p className="mt-2 text-xs text-danger">
                No Grievance Officer is published, so escalations cannot be
                delivered.
              </p>
            )}
          </div>

          {/* Consents lapsing this week */}
          <div className="px-5 py-4">
            <p className="text-sm font-medium text-ink">
              Consents expiring in the next 7 days ({attention.consents_expiring_7.length})
            </p>
            {attention.consents_expiring_7.length === 0 ? (
              <p className="mt-1 text-sm text-muted">None expiring this week.</p>
            ) : (
              <ul className="mt-2 space-y-2">
                {attention.consents_expiring_7.map((c) => (
                  <li key={c.id}
                      className="flex flex-wrap items-center gap-3 rounded-lg border border-warning/40 bg-warning/5 px-3 py-2 text-sm">
                    <span className="text-ink">{c.purpose}</span>
                    <span className="text-muted">{c.user_id}</span>
                    <span className="ml-auto text-muted">
                      expires {new Date(c.expires_at).toLocaleDateString()}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </section>

      {/* ---------------------------------------------------- quick links -- */}
      <div className="flex flex-wrap gap-3">
        <Link to="/admin/dsar" className="btn-secondary">DSAR Queue</Link>
        <Link to="/admin/grievances" className="btn-secondary">Grievance Queue</Link>
        <Link to="/admin/audit" className="btn-secondary">Audit Logs</Link>
        <Link to="/admin/retention" className="btn-secondary">Retention Policy</Link>
      </div>
    </div>
  );
}
