// ============================================================================
// User Role Management (/admin/roles)
// Users and their roles, the permission matrix, MFA per user, SSO config, and a
// role-change audit log. Revoking access is destructive → ConfirmModal.
// ============================================================================
import { useEffect, useState } from "react";
import { addUser, getAuditLogs, getUsers, ROLE_PERMISSIONS, ROLES, updateUser } from "../../api";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";
import ConfirmModal from "../../components/common/ConfirmModal";
import AuditHashBadge from "../../components/common/AuditHashBadge";

const ROLE_LABEL = {
  data_principal: "Data Principal",
  admin: "Admin / DPO",
  auditor: "Auditor",
  grievance_officer: "Grievance Officer",
};

export default function UserRoleManagement() {
  const { notify } = useApp();
  const [users, setUsers] = useState([]);
  const [roleLog, setRoleLog] = useState([]);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ name: "", email: "", role: "admin" });
  const [editing, setEditing] = useState(null);
  const [revoking, setRevoking] = useState(null);
  const [busy, setBusy] = useState(false);
  const [sso, setSso] = useState({ enabled: false, provider: "Azure AD (Entra ID)", domain: "example.com" });

  const load = async () => {
    setUsers(await getUsers());
    const logs = await getAuditLogs();
    setRoleLog(logs.filter((l) => ["role_changed", "user_created", "access_revoked", "user_updated"].includes(l.action_type)));
  };

  useEffect(() => {
    load();
  }, []);

  const create = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      await addUser(form);
      await load();
      setForm({ name: "", email: "", role: "admin" });
      setAdding(false);
      notify("User added and the change logged.");
    } finally {
      setBusy(false);
    }
  };

  const changeRole = async (id, role) => {
    await updateUser(id, { role });
    await load();
    setEditing(null);
    notify("Role changed. The change is in the audit trail.");
  };

  const toggleMfa = async (u) => {
    await updateUser(u.id, { mfa: !u.mfa });
    await load();
    notify(u.mfa ? "MFA requirement removed." : "MFA now enforced for this user.");
  };

  const revoke = async () => {
    setBusy(true);
    try {
      await updateUser(revoking.id, { active: false });
      await load();
      notify("Access revoked.");
    } finally {
      setBusy(false);
      setRevoking(null);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">Users and roles</h1>
          <p className="text-sm text-muted">
            Who can see and do what. Every role change is written to the audit trail.
          </p>
        </div>
        <button type="button" className="btn-primary" onClick={() => setAdding((v) => !v)}>
          {adding ? "Cancel" : "Add user"}
        </button>
      </div>

      {adding && (
        <form onSubmit={create} className="card grid gap-3 p-5 sm:grid-cols-4">
          <div>
            <label className="label" htmlFor="u-name">Name</label>
            <input id="u-name" className="input" value={form.name} required
                   onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </div>
          <div>
            <label className="label" htmlFor="u-email">Email</label>
            <input id="u-email" type="email" className="input" value={form.email} required
                   onChange={(e) => setForm({ ...form, email: e.target.value })} />
          </div>
          <div>
            <label className="label" htmlFor="u-role">Role</label>
            <select id="u-role" className="input" value={form.role}
                    onChange={(e) => setForm({ ...form, role: e.target.value })}>
              {ROLES.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
            </select>
          </div>
          <div className="flex items-end">
            <button type="submit" className="btn-primary w-full" disabled={busy}>
              {busy ? "Adding…" : "Create user"}
            </button>
          </div>
        </form>
      )}

      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-line">
            <thead className="bg-canvas">
              <tr>
                <th className="th">Name</th>
                <th className="th">Email</th>
                <th className="th">Role</th>
                <th className="th">Created</th>
                <th className="th">MFA</th>
                <th className="th">Status</th>
                <th className="th sr-only">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {users.map((u) => (
                <tr key={u.id} className={u.active ? "" : "opacity-60"}>
                  <td className="td">{u.name}</td>
                  <td className="td text-xs text-muted">{u.email}</td>
                  <td className="td">
                    {editing === u.id ? (
                      <select className="input py-1 text-sm" defaultValue={u.role}
                              onChange={(e) => changeRole(u.id, e.target.value)}>
                        {ROLES.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
                      </select>
                    ) : (
                      <span className="tag">{ROLE_LABEL[u.role] || u.role}</span>
                    )}
                  </td>
                  <td className="td text-xs text-muted">{u.created_at}</td>
                  <td className="td">
                    <button type="button" role="switch" aria-checked={u.mfa}
                            aria-label={`MFA for ${u.name}`} onClick={() => toggleMfa(u)}
                            className={`relative inline-flex h-5 w-9 items-center rounded-full transition ${
                              u.mfa ? "bg-teal" : "bg-line"
                            }`}>
                      <span className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow transition ${
                        u.mfa ? "translate-x-5" : "translate-x-1"
                      }`} />
                    </button>
                  </td>
                  <td className="td">
                    <StatusBadge status={u.active ? "active" : "withdrawn"}
                                 label={u.active ? "Active" : "Revoked"} />
                  </td>
                  <td className="td">
                    <div className="flex gap-2">
                      <button type="button" className="text-sm text-teal underline"
                              onClick={() => setEditing(editing === u.id ? null : u.id)}>
                        {editing === u.id ? "Done" : "Edit role"}
                      </button>
                      {u.active && (
                        <button type="button" className="text-sm text-danger underline"
                                onClick={() => setRevoking(u)}>
                          Revoke
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* --------------------------------------------- permission matrix -- */}
      <section className="card overflow-hidden">
        <div className="border-b border-line px-5 py-4">
          <h2 className="font-semibold text-ink">Role permission matrix</h2>
          <p className="text-xs text-muted">What each role can see and do.</p>
        </div>
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-line">
            <thead className="bg-canvas">
              <tr>
                <th className="th">Capability</th>
                <th className="th text-center">Data Principal</th>
                <th className="th text-center">Admin / DPO</th>
                <th className="th text-center">Auditor</th>
                <th className="th text-center">Grievance Officer</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {ROLE_PERMISSIONS.map((p) => (
                <tr key={p.capability}>
                  <td className="td">{p.capability}</td>
                  {["data_principal", "admin", "auditor", "grievance_officer"].map((r) => (
                    <td key={r} className="td text-center">
                      {p[r] ? (
                        <span className="text-success" title="Allowed">✓ <span className="sr-only">allowed</span></span>
                      ) : (
                        <span className="text-muted" title="Not allowed">— <span className="sr-only">not allowed</span></span>
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* -------------------------------------------------------- SSO ----- */}
      <section className="card p-5">
        <h2 className="font-semibold text-ink">Single sign-on</h2>
        <p className="text-xs text-muted">
          Configuration only in this build — no identity provider is contacted.
        </p>
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          <div>
            <label className="label" htmlFor="sso-provider">Provider</label>
            <select id="sso-provider" className="input" value={sso.provider}
                    onChange={(e) => setSso({ ...sso, provider: e.target.value })}>
              {["Azure AD (Entra ID)", "Okta", "Google Workspace", "Keycloak"].map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="sso-domain">Allowed email domain</label>
            <input id="sso-domain" className="input" value={sso.domain}
                   onChange={(e) => setSso({ ...sso, domain: e.target.value })} />
          </div>
          <div className="flex items-end">
            <label className="flex items-center gap-3">
              <button type="button" role="switch" aria-checked={sso.enabled} aria-label="Enable SSO"
                      onClick={() => setSso({ ...sso, enabled: !sso.enabled })}
                      className={`relative inline-flex h-6 w-11 items-center rounded-full transition ${
                        sso.enabled ? "bg-teal" : "bg-line"
                      }`}>
                <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition ${
                  sso.enabled ? "translate-x-6" : "translate-x-1"
                }`} />
              </button>
              <span className="text-sm text-ink">{sso.enabled ? "Enabled" : "Disabled"}</span>
            </label>
          </div>
        </div>
      </section>

      {/* --------------------------------------------- role change log ---- */}
      <section className="card overflow-hidden">
        <div className="border-b border-line px-5 py-4">
          <h2 className="font-semibold text-ink">Role change log</h2>
          <p className="text-xs text-muted">Every role change, creation and revocation.</p>
        </div>
        {roleLog.length === 0 ? (
          <p className="px-5 py-4 text-sm text-muted">Nothing recorded yet.</p>
        ) : (
          <ul className="divide-y divide-line">
            {roleLog.map((l) => (
              <li key={l.id} className="flex flex-wrap items-center gap-3 px-5 py-3 text-sm">
                <span className="font-mono text-xs">{l.log_id}</span>
                <span className="tag">{l.action_type}</span>
                <span className="font-mono text-xs">{l.user_id}</span>
                <span className="text-xs text-muted">{new Date(l.timestamp).toLocaleString()}</span>
                <span className="text-xs text-muted">by {l.initiator}</span>
                <AuditHashBadge hash={l.audit_hash} chars={10} />
              </li>
            ))}
          </ul>
        )}
      </section>

      <ConfirmModal
        open={Boolean(revoking)}
        title={`Revoke access for ${revoking?.name}?`}
        body="They will be signed out and unable to sign back in."
        consequences={[
          "All active sessions for this user are ended.",
          "Any queue items assigned to them stay assigned and need reassigning.",
          "The revocation is written to the audit trail.",
        ]}
        confirmLabel="Revoke access"
        busy={busy}
        onCancel={() => setRevoking(null)}
        onConfirm={revoke}
      />
    </div>
  );
}
