// ============================================================================
// Users & roles (/admin/roles)
//
// Real, against /v1/admin. The previous version rendered MOCK_USERS_ADMIN with
// locked buttons and a hardcoded permissions table.
//
// Four things this screen is careful about:
//
//   * **Nobody sets anybody else's password.** People are invited and choose
//     their own. An administrator who knows a colleague's password makes every
//     audit entry attributed to that colleague arguable — and the audit chain is
//     what this whole product is for.
//   * **The invite link is shown once.** It is emailed too, but the default
//     notification provider writes to a log rather than sending, so the link is
//     displayed. Losing it means revoking and re-inviting, which the copy says.
//   * **The capability matrix comes from the API.** A permissions table
//     hardcoded here could disagree with the code enforcing it, which would tell
//     an administrator their workspace is configured one way while it behaves
//     another.
//   * **A demotion or deactivation signs them out everywhere**, and the confirm
//     dialog says so before the click rather than after.
// ============================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  capabilities as fetchCapabilities,
  changeRole,
  deactivateUser,
  reactivateUser,
  invite,
  listInvitations,
  listSessions,
  listUsers,
  revokeInvitation,
  revokeSessions,
  ROLE_BLURBS,
  ROLE_LABELS,
} from "../../api/users";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";
import ConfirmModal from "../../components/common/ConfirmModal";
import SlideOver from "../../components/common/SlideOver";

const ROLES = ["admin", "auditor", "grievance_officer", "data_principal"];

function fmt(iso) {
  return iso ? new Date(iso).toLocaleString() : "—";
}

/** A revealed-once secret. Copyable, and honest about not coming back. */
function OnceOnly({ url, emailed, onDone }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="rounded-lg border border-warning/50 bg-warning/10 p-4">
      <p className="text-sm font-semibold text-ink">
        This link is shown once
      </p>
      <p className="mt-1 text-xs text-muted">
        {emailed
          ? "It has also been emailed. If your notification provider is the default one, that email went to the server log rather than an inbox — so copy it now."
          : "The email could not be sent, so this is the only copy. The invitation itself is recorded and valid."}
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <code className="min-w-0 flex-1 overflow-x-auto rounded border border-line bg-surface px-2 py-1.5 text-[11px] text-ink">
          {url}
        </code>
        <button
          type="button"
          className="btn-secondary"
          onClick={() => {
            navigator.clipboard?.writeText(url);
            setCopied(true);
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <p className="mt-2 text-xs text-muted">
        Nothing stores it in a form we can read back. If it is lost, revoke the
        invitation and send a new one.
      </p>
      <button type="button" className="btn-ghost mt-2" onClick={onDone}>
        I have it
      </button>
    </div>
  );
}

export default function UserRoleManagement() {
  const { notify, user: me } = useApp();

  const [users, setUsers] = useState([]);
  const [invitations, setInvitations] = useState([]);
  const [matrix, setMatrix] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // invite form
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("auditor");
  const [issued, setIssued] = useState(null);

  // dialogs
  const [confirmRole, setConfirmRole] = useState(null); // {user, role}
  const [confirmDeactivate, setConfirmDeactivate] = useState(null);
  const [confirmRevokeSessions, setConfirmRevokeSessions] = useState(null);
  const [sessionsFor, setSessionsFor] = useState(null);
  const [sessions, setSessions] = useState([]);

  const load = useCallback(async () => {
    try {
      const [u, inv] = await Promise.all([listUsers(), listInvitations()]);
      setUsers(u);
      setInvitations(inv);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
    fetchCapabilities().then(setMatrix).catch(() => setMatrix(null));
  }, [load]);

  useEffect(() => {
    if (!sessionsFor) return;
    listSessions(sessionsFor.id).then(setSessions).catch(() => setSessions([]));
  }, [sessionsFor]);

  const act = async (fn, message) => {
    setBusy(true);
    setError("");
    try {
      const out = await fn();
      await load();
      if (message) notify(message);
      return out;
    } catch (e) {
      setError(e.message);
      return null;
    } finally {
      setBusy(false);
      setConfirmRole(null);
      setConfirmDeactivate(null);
      setConfirmRevokeSessions(null);
    }
  };

  const activeAdmins = useMemo(
    () => users.filter((u) => u.role === "admin"),
    [users],
  );

  const pending = invitations.filter((i) => i.status === "pending");
  const settled = invitations.filter((i) => i.status !== "pending");

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">Users &amp; roles</h1>
        <p className="text-sm text-muted">
          People are invited and choose their own password. Nobody here — including
          you — can set or see somebody else&rsquo;s.
        </p>
      </div>

      {error && (
        <div className="rounded-lg border border-danger/50 bg-danger/10 p-3 text-sm text-ink">
          {error}
        </div>
      )}

      {/* ----------------------------------------------------------- invite -- */}
      <section className="card p-5">
        <h2 className="font-semibold text-ink">Invite somebody</h2>
        <p className="text-xs text-muted">
          They receive a single-use link, valid for 72 hours, and set their own
          password. You choose the role — they cannot.
        </p>
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <div className="min-w-[16rem] flex-1">
            <label className="label" htmlFor="inv-email">Email</label>
            <input
              id="inv-email"
              className="input"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="colleague@company.com"
            />
          </div>
          <div className="min-w-[12rem]">
            <label className="label" htmlFor="inv-role">Role</label>
            <select
              id="inv-role"
              className="input"
              value={role}
              onChange={(e) => setRole(e.target.value)}
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>{ROLE_LABELS[r]}</option>
              ))}
            </select>
          </div>
          <button
            type="button"
            className="btn-primary"
            disabled={busy || !email.includes("@")}
            onClick={() =>
              act(async () => {
                const out = await invite({ email, role });
                setIssued(out);
                setEmail("");
                return out;
              }, "Invitation sent.")
            }
          >
            {busy ? "Sending…" : "Send invitation"}
          </button>
        </div>
        <p className="mt-2 text-xs text-muted">{ROLE_BLURBS[role]}</p>

        {issued && (
          <div className="mt-4">
            <OnceOnly
              url={issued.accept_url}
              emailed={issued.emailed}
              onDone={() => setIssued(null)}
            />
          </div>
        )}
      </section>

      {/* ------------------------------------------------------------ users -- */}
      <section className="card overflow-hidden">
        <div className="border-b border-line px-5 py-3">
          <h2 className="font-semibold text-ink">
            People in this workspace ({users.length})
          </h2>
          {activeAdmins.length === 1 && (
            <p className="text-xs text-warning">
              There is one administrator. A workspace must keep at least one — the
              last one cannot be demoted or deactivated, because a workspace with
              no administrator cannot be recovered without our support team.
            </p>
          )}
        </div>
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-line">
            <thead className="bg-canvas">
              <tr>
                <th className="th">Name</th>
                <th className="th">Email</th>
                <th className="th">Role</th>
                <th className="th">MFA</th>
                <th className="th">Last signed in</th>
                <th className="th sr-only">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {users.length === 0 && (
                <tr>
                  <td className="td text-center text-muted" colSpan={6}>
                    Loading…
                  </td>
                </tr>
              )}
              {users.map((u) => {
                const isMe = u.id === me?.id;
                return (
                  <tr key={u.id} className={u.is_active ? "" : "opacity-60"}>
                    <td className="td">
                      {u.full_name}
                      {isMe && <span className="ml-1 tag">you</span>}
                      {/* Stated on the row, not left to be inferred from which
                          button is showing. A revoked account that looks exactly
                          like an active one is how "revoke does not work" gets
                          reported for a feature that works. */}
                      {!u.is_active && (
                        <span className="ml-1 inline-flex items-center gap-1 rounded-full border border-line px-2 py-0.5 text-xs text-danger">
                          <span className="h-2 w-2 rounded-full bg-danger" aria-hidden="true" />
                          access revoked
                        </span>
                      )}
                    </td>
                    <td className="td text-xs text-muted">{u.email}</td>
                    <td className="td">
                      <select
                        className="input py-1 text-xs"
                        value={u.role}
                        // You cannot change your own role — the server refuses it
                        // too, so that a mis-click cannot lock a workspace out of
                        // its own console.
                        disabled={busy || isMe || !u.is_active}
                        onChange={(e) =>
                          setConfirmRole({ user: u, role: e.target.value })
                        }
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r}>{ROLE_LABELS[r]}</option>
                        ))}
                      </select>
                    </td>
                    <td className="td text-xs">
                      {u.mfa_enabled ? (
                        <span className="text-success">on</span>
                      ) : (
                        <span className="text-muted">off</span>
                      )}
                    </td>
                    <td className="td text-xs text-muted">{fmt(u.last_login_at)}</td>
                    <td className="td">
                      <div className="flex flex-wrap gap-2 text-xs">
                        <button
                          type="button"
                          className="text-teal underline"
                          onClick={() => setSessionsFor(u)}
                        >
                          Sessions
                        </button>
                        {/* One button or the other, driven by `is_active`.
                            Before the API returned that field this always said
                            "Revoke access" — including for accounts already
                            revoked — so the click appeared to do nothing and the
                            table gave no sign the first one had worked. */}
                        {!isMe && u.is_active && (
                          <button
                            type="button"
                            className="text-danger underline"
                            onClick={() => setConfirmDeactivate(u)}
                          >
                            Revoke access
                          </button>
                        )}
                        {!isMe && !u.is_active && (
                          <button
                            type="button"
                            className="text-teal underline"
                            onClick={async () => {
                              try {
                                await reactivateUser(u.id);
                                notify(`${u.full_name} can sign in again.`);
                                await load();
                              } catch (err) {
                                notify(err.message, "error");
                              }
                            }}
                          >
                            Restore access
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      {/* ------------------------------------------------------ invitations -- */}
      <section className="card overflow-hidden">
        <div className="border-b border-line px-5 py-3">
          <h2 className="font-semibold text-ink">
            Invitations{pending.length > 0 && ` — ${pending.length} waiting`}
          </h2>
          <p className="text-xs text-muted">
            Nothing is deleted here. An invitation that was withdrawn or that
            lapsed stays on the record, because the fact a credential was issued
            outlives the credential.
          </p>
        </div>
        {invitations.length === 0 ? (
          <p className="px-5 py-8 text-center text-sm text-muted">
            No invitations yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-line">
              <thead className="bg-canvas">
                <tr>
                  <th className="th">Email</th>
                  <th className="th">Role</th>
                  <th className="th">Status</th>
                  <th className="th">Expires</th>
                  <th className="th sr-only">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {[...pending, ...settled].map((i) => (
                  <tr key={i.id} className={i.status === "pending" ? "" : "opacity-70"}>
                    <td className="td text-xs">{i.email}</td>
                    <td className="td text-xs">{ROLE_LABELS[i.role] || i.role}</td>
                    <td className="td">
                      <StatusBadge
                        status={
                          i.status === "accepted"
                            ? "completed"
                            : i.status === "pending"
                              ? "pending"
                              : "none"
                        }
                        label={i.status}
                      />
                      {i.revoked_reason && (
                        <p className="mt-1 text-xs text-muted">{i.revoked_reason}</p>
                      )}
                    </td>
                    <td className="td text-xs text-muted">{fmt(i.expires_at)}</td>
                    <td className="td">
                      {i.status === "pending" && (
                        <button
                          type="button"
                          className="text-xs text-danger underline"
                          disabled={busy}
                          onClick={() =>
                            act(
                              () => revokeInvitation(i.id),
                              "Invitation withdrawn.",
                            )
                          }
                        >
                          Withdraw
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* -------------------------------------------------- capability matrix -- */}
      <section className="card overflow-hidden">
        <div className="border-b border-line px-5 py-3">
          <h2 className="font-semibold text-ink">What each role may do</h2>
          <p className="text-xs text-muted">
            {matrix?.note ||
              "Read from the API, not restated here — a permissions table that can disagree with the code enforcing it is worse than none."}
          </p>
        </div>
        {!matrix ? (
          <p className="px-5 py-8 text-center text-sm text-muted">
            Could not load the capability matrix.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-line">
              <thead className="bg-canvas">
                <tr>
                  <th className="th">Capability</th>
                  {matrix.roles.map((r) => (
                    <th key={r} className="th text-center">{ROLE_LABELS[r] || r}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {matrix.capabilities.map((cap) => (
                  <tr key={cap}>
                    <td className="td font-mono text-xs">{cap}</td>
                    {matrix.roles.map((r) => (
                      <td key={r} className="td text-center">
                        {matrix.matrix[r].includes(cap) ? (
                          <span className="text-success" aria-label="yes">✓</span>
                        ) : (
                          <span className="text-muted" aria-label="no">·</span>
                        )}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* --------------------------------------------------------- sessions -- */}
      <SlideOver
        open={Boolean(sessionsFor)}
        title={sessionsFor ? `Sessions — ${sessionsFor.full_name}` : ""}
        subtitle={sessionsFor?.email}
        onClose={() => setSessionsFor(null)}
      >
        {sessionsFor && (
          <div className="space-y-4">
            <p className="text-xs text-muted">
              One entry per browser. Ending them does not lock the account — their
              password still works. Use &ldquo;Revoke access&rdquo; for that.
            </p>
            {sessions.length === 0 ? (
              <p className="text-sm text-muted">No live sessions.</p>
            ) : (
              <ul className="space-y-2">
                {sessions.map((s) => (
                  <li key={s.family_id} className="rounded-lg border border-line p-3 text-xs">
                    <p className="text-ink">{s.user_agent || "unknown device"}</p>
                    <p className="mt-1 text-muted">
                      {s.ip_address || "no address recorded"} · started{" "}
                      {fmt(s.started_at)} · last used {fmt(s.last_used_at)}
                    </p>
                    <p className="mt-0.5 text-muted">
                      expires {fmt(s.expires_at)} · {s.rotations} refresh
                      {s.rotations === 1 ? "" : "es"}
                    </p>
                  </li>
                ))}
              </ul>
            )}
            {sessions.length > 0 && (
              <button
                type="button"
                className="btn-secondary w-full"
                disabled={busy}
                onClick={() => setConfirmRevokeSessions(sessionsFor)}
              >
                Sign out of all {sessions.length} session
                {sessions.length === 1 ? "" : "s"}
              </button>
            )}
          </div>
        )}
      </SlideOver>

      {/* ----------------------------------------------------------- dialogs -- */}
      <ConfirmModal
        open={Boolean(confirmRole)}
        title={
          confirmRole
            ? `Change ${confirmRole.user.full_name} to ${ROLE_LABELS[confirmRole.role]}?`
            : ""
        }
        body={confirmRole ? ROLE_BLURBS[confirmRole.role] : ""}
        consequences={
          confirmRole
            ? [
                confirmRole.user.role === "admin" && confirmRole.role !== "admin"
                  ? "They will be signed out of every device immediately — a demotion has to mean now, not when their session happens to expire."
                  : "This takes effect on their next request; the role is re-read every time.",
                "The change is written to the audit trail.",
              ]
            : []
        }
        confirmLabel="Change the role"
        destructive={confirmRole?.user.role === "admin"}
        busy={busy}
        onCancel={() => setConfirmRole(null)}
        onConfirm={() =>
          act(
            () => changeRole(confirmRole.user.id, confirmRole.role),
            `${confirmRole.user.full_name} is now ${ROLE_LABELS[confirmRole.role]}.`,
          )
        }
      />

      <ConfirmModal
        open={Boolean(confirmDeactivate)}
        title={confirmDeactivate ? `Revoke ${confirmDeactivate.full_name}'s access?` : ""}
        body="They will not be able to sign in, and every live session ends immediately."
        consequences={[
          "Signed out of every device now, not when their session expires.",
          "Their record and everything they did is kept — the audit trail does not change.",
          "You can restore access later by changing their role; nothing is deleted.",
        ]}
        confirmLabel="Revoke access"
        busy={busy}
        onCancel={() => setConfirmDeactivate(null)}
        onConfirm={() =>
          act(
            () => deactivateUser(confirmDeactivate.id),
            `${confirmDeactivate.full_name} can no longer sign in.`,
          )
        }
      />

      <ConfirmModal
        open={Boolean(confirmRevokeSessions)}
        title="Sign them out everywhere?"
        body="Every live session ends immediately. Their password still works, so this is not a lockout."
        consequences={[
          "They will have to sign in again on every device.",
          "Use this when a laptop is lost or a session looks wrong.",
        ]}
        confirmLabel="Sign out everywhere"
        destructive={false}
        busy={busy}
        onCancel={() => setConfirmRevokeSessions(null)}
        onConfirm={() =>
          act(async () => {
            const out = await revokeSessions(confirmRevokeSessions.id);
            setSessions([]);
            return out;
          }, "Signed out of every device.")
        }
      />
    </div>
  );
}
