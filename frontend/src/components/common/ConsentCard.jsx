// ============================================================================
// ConsentCard — one purpose, with its toggle. Shared by the Consent Banner and
// the Preference Centre, which is why it takes a `variant`.
//
// Two rules from the brief are enforced here rather than left to the caller:
//   • no pre-checked optional toggles — `checked` must be passed in, and the
//     banner passes false for everything optional;
//   • mandatory purposes render as a LOCKED toggle with the reason visible,
//     never hidden and never silently switched on.
// ============================================================================
import StatusBadge from "./StatusBadge";

function Toggle({ checked, disabled, onChange, label }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange?.(!checked)}
      className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition
        ${checked ? "bg-teal" : "bg-line"}
        ${disabled ? "cursor-not-allowed opacity-60" : ""}`}
    >
      <span
        className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition
          ${checked ? "translate-x-6" : "translate-x-1"}`}
      />
    </button>
  );
}

export default function ConsentCard({
  notice,
  consent,
  checked,
  onChange,
  variant = "banner",   // "banner" | "preference"
  onHistory,
  daysToExpiry,
}) {
  const mandatory = notice?.mandatory;
  const expiringSoon = typeof daysToExpiry === "number" && daysToExpiry >= 0 && daysToExpiry < 30;

  return (
    <div className="card p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-semibold text-ink">{notice.purpose}</h3>
            {mandatory ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-danger/10 px-2 py-0.5 text-xs font-semibold text-danger">
                <span className="h-1.5 w-1.5 rounded-full bg-danger" aria-hidden="true" />
                MANDATORY
              </span>
            ) : (
              <span className="tag">OPTIONAL</span>
            )}
            <span className="tag">{notice.category}</span>
            {variant === "preference" && consent && <StatusBadge status={consent.status} />}
          </div>

          <p className="mt-2 text-sm text-ink">{notice.content}</p>

          <dl className="mt-3 grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
            <div>
              <dt className="font-semibold text-muted">Data collected</dt>
              <dd className="text-ink">{notice.data_collected}</dd>
            </div>
            <div>
              <dt className="font-semibold text-muted">Retention period</dt>
              <dd className="text-ink">
                Kept for {Math.round(notice.retention_days / 365)} year
                {notice.retention_days >= 730 ? "s" : ""} ({notice.retention_days} days)
              </dd>
            </div>
            <div>
              <dt className="font-semibold text-muted">Your rights</dt>
              <dd className="text-ink">{notice.user_rights}</dd>
            </div>
            <div>
              <dt className="font-semibold text-muted">What happens if you say no</dt>
              <dd className="text-ink">{notice.withdrawal_policy}</dd>
            </div>
          </dl>

          {variant === "preference" && consent && (
            <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
              <span>
                Given {new Date(consent.given_at).toLocaleDateString()} · v{consent.version} ·{" "}
                {consent.method}
              </span>
              {consent.expires_at && (
                <span className={expiringSoon ? "font-semibold text-warning" : ""}>
                  Expires {new Date(consent.expires_at).toLocaleDateString()}
                  {expiringSoon ? ` — in ${daysToExpiry} days` : ""}
                </span>
              )}
              {consent.withdrawn_at && (
                <span>Withdrawn {new Date(consent.withdrawn_at).toLocaleDateString()}</span>
              )}
              {onHistory && (
                <button type="button" className="text-teal underline" onClick={onHistory}>
                  Consent history
                </button>
              )}
            </div>
          )}
        </div>

        <div className="flex flex-col items-end gap-2">
          <Toggle
            checked={mandatory ? true : checked}
            disabled={mandatory}
            onChange={onChange}
            label={`Consent for ${notice.purpose}`}
          />
          <span className="text-xs text-muted">
            {mandatory ? "Locked" : checked ? "ON" : "OFF"}
          </span>
        </div>
      </div>

      {mandatory && (
        <p className="mt-3 rounded-lg border border-line bg-canvas px-3 py-2 text-xs text-muted">
          <strong className="text-ink">Required by law.</strong> This purpose cannot be switched
          off while you hold an account — {notice.withdrawal_policy.toLowerCase()}
        </p>
      )}
    </div>
  );
}
