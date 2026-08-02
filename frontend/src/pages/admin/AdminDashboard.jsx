// ============================================================================
// Admin Dashboard (/admin/dashboard)
// The screen a DPO opens every morning: 6 stats, 3 charts, and a
// "needs immediate attention" block that surfaces anything at risk of breaching
// a statutory deadline.
// ============================================================================
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getAdminDashboard, GRIEVANCE_ESCALATION_DAYS } from "../../api";
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

  useEffect(() => {
    getAdminDashboard().then(setData);
  }, []);

  if (!data) return <p className="text-sm text-muted">Loading compliance summary…</p>;

  const { stats, attention, charts } = data;

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
            <strong className="font-semibold">Data request figures are real.</strong>{" "}
            Consent and grievance figures on this page are sample data — those
            modules are still in preview.{" "}
            <Link to="/roadmap" className="text-teal underline">
              See what is live today
            </Link>
          </p>
        </div>
      </div>

      {/* ---------------------------------------------------------- stats -- */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <StatCard label="Total active consents" value={stats.active_consents} tone="success" sample />
        <StatCard label="Withdrawn this month" value={stats.withdrawn_this_month} tone="neutral" sample />
        <StatCard label="Open DSAR requests" value={stats.open_dsar} tone="info" to="/admin/dsar" />
        <StatCard
          label="DSAR overdue"
          value={stats.overdue_dsar}
          tone={stats.overdue_dsar > 0 ? "danger" : "neutral"}
          to="/admin/dsar"
          badge={
            stats.overdue_dsar > 0 ? (
              <span className="rounded-full bg-danger px-2 py-0.5 text-[10px] font-bold text-white">
                ACTION
              </span>
            ) : null
          }
        />
        <StatCard label="Open grievances" value={stats.open_grievances} tone="warning" to="/admin/grievances" sample />
        <StatCard label="Expiring in 30 days" value={stats.expiring_30} tone="warning" sample />
      </div>

      {/* --------------------------------------------------------- charts -- */}
      <div className="grid gap-5 lg:grid-cols-2">
        <section className="card p-5">
          <h2 className="font-semibold text-ink">Data requests by type</h2>
          <p className="text-xs text-muted">All requests on this account</p>
          <div className="mt-4">
            {charts.dsar_by_type.length ? (
              <BarChart data={charts.dsar_by_type} />
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
              Data requests due within 5 days ({attention.dsar_due_soon.length})
            </p>
            {attention.dsar_due_soon.length === 0 ? (
              <p className="mt-1 text-sm text-muted">Nothing at risk.</p>
            ) : (
              <ul className="mt-2 space-y-2">
                {attention.dsar_due_soon.map((r) => {
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

          {/* Grievances beyond the escalation threshold */}
          <div className="px-5 py-4">
            <p className="text-sm font-medium text-ink">
              Grievances open more than {GRIEVANCE_ESCALATION_DAYS} days (
              {attention.stale_grievances.length})
            </p>
            {attention.stale_grievances.length === 0 ? (
              <p className="mt-1 text-sm text-muted">Nothing overdue.</p>
            ) : (
              <ul className="mt-2 space-y-2">
                {attention.stale_grievances.map((g) => (
                  <li key={g.id}
                      className="flex flex-wrap items-center gap-3 rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm">
                    <span className="font-mono text-xs">{g.reference}</span>
                    <span className="text-ink">{g.category}</span>
                    <span className="text-muted">
                      {Math.floor((Date.now() - new Date(g.submitted_at)) / DAY)} days open
                    </span>
                    <StatusBadge status={g.status} />
                    <Link to="/admin/grievances" className="ml-auto text-teal underline">Open</Link>
                  </li>
                ))}
              </ul>
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
