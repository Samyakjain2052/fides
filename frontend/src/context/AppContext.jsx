// ============================================================================
// Session state: who is signed in, and which language they are reading in.
// Both persist to localStorage so a refresh doesn't sign you out.
// useState + useContext only — no Redux, per the brief.
// ============================================================================
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { LANGUAGES, ROLES, setSubjectIdentity } from "../api";
import { clearSession, logout as apiLogout, restoreSession } from "../api/auth";

const AppContext = createContext(null);

const STORE_USER = "datashield.user";
const STORE_LANG = "datashield.language";

// Translation scaffolding. The brief defers real translation ("language
// switcher UI ready, real translation backend wires later" — Bhashini), so
// English is complete, Hindi is a worked sample proving the mechanism, and
// every other language falls back to English rather than showing blanks.
const DICTIONARY = {
  English: {},
  Hindi: {
    "Welcome": "स्वागत है",
    "My Consent Preferences": "मेरी सहमति प्राथमिकताएँ",
    "Active Consents": "सक्रिय सहमतियाँ",
    "Pending Data Requests": "लंबित डेटा अनुरोध",
    "Open Grievances": "खुली शिकायतें",
    "Expiring Soon": "जल्द समाप्त",
    "Manage My Consents": "मेरी सहमतियाँ प्रबंधित करें",
    "Submit a Data Request": "डेटा अनुरोध सबमिट करें",
    "File a Complaint": "शिकायत दर्ज करें",
    "Save My Choices": "मेरी पसंद सहेजें",
    "Accept All Optional": "सभी वैकल्पिक स्वीकारें",
    "Decline All Optional": "सभी वैकल्पिक अस्वीकारें",
    "Recent Activity": "हाल की गतिविधि",
    "Mandatory": "अनिवार्य",
    "Optional": "वैकल्पिक",
    "Active": "सक्रिय",
    "Withdrawn": "वापस लिया",
    "Expired": "समाप्त",
    "Cancel": "रद्द करें",
    "Access My Data": "मेरा डेटा देखें",
    "Correct My Data": "मेरा डेटा सुधारें",
    "Erase My Data": "मेरा डेटा मिटाएँ",
  },
};

export function AppProvider({ children }) {
  // The user is NOT read from localStorage any more.
  //
  // It used to be, which meant anyone could open devtools, write
  // {"role":"admin"} into localStorage and reload into the admin console. The
  // session now comes from the server: an HttpOnly refresh cookie the page
  // cannot read, exchanged for a short-lived access token held in memory.
  const [user, setUser] = useState(null);
  const [capabilities, setCapabilities] = useState([]);
  // null = still checking. The router waits for this so a signed-in user is not
  // flashed the login screen on every reload.
  const [authReady, setAuthReady] = useState(false);
  const [language, setLanguage] = useState(
    () => localStorage.getItem(STORE_LANG) || "English"
  );
  const [toast, setToast] = useState(null);

  // On load, try to restore the session from the refresh cookie. Failure is the
  // normal "nobody is signed in" case, not an error.
  useEffect(() => {
    let cancelled = false;
    restoreSession()
      .then((session) => {
        if (cancelled) return;
        if (session) {
          setUser(session.user);
          setCapabilities(session.capabilities || []);
        }
      })
      .finally(() => !cancelled && setAuthReady(true));
    return () => {
      cancelled = true;
    };
  }, []);

  // Clear the stale key from the previous localStorage-based build, so an old
  // tab does not leave a forged user object lying around.
  useEffect(() => {
    localStorage.removeItem(STORE_USER);
  }, []);

  // Keep the API layer's Data Principal in step with the real session. The DSAR
  // path executes for real now, so acting as the wrong identity is not a
  // cosmetic bug — it would run one person's erasure against another's records.
  useEffect(() => {
    setSubjectIdentity(user);
  }, [user]);

  useEffect(() => {
    localStorage.setItem(STORE_LANG, language);
  }, [language]);

  // Auto-dismiss toasts; the brief asks for a success toast after consent changes.
  useEffect(() => {
    if (!toast) return undefined;
    const id = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(id);
  }, [toast]);

  const t = useCallback(
    (text) => (DICTIONARY[language] && DICTIONARY[language][text]) || text,
    [language]
  );

  const value = useMemo(
    () => ({
      user,
      authReady,
      capabilities,
      /** Adopt a session returned by login or register. */
      signIn: (session) => {
        setUser(session.user);
        setCapabilities(session.capabilities || []);
      },
      signOut: async () => {
        await apiLogout();   // revokes the whole refresh-token family server-side
        clearSession();
        setUser(null);
        setCapabilities([]);
      },
      /** Server-granted capability check. The server re-checks on every request. */
      can: (capability) => capabilities.includes(capability),
      role: user?.role || null,
      roleLabel: ROLES.find((r) => r.id === user?.role)?.label || "",
      language,
      setLanguage,
      languages: LANGUAGES,
      t,
      // `translated` is false for languages with no dictionary yet, so screens
      // can be honest about it instead of silently showing English.
      translated: Boolean(DICTIONARY[language]),
      toast,
      notify: (message, tone = "success") => setToast({ message, tone }),
    }),
    [user, authReady, capabilities, language, t, toast]
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}

export function useApp() {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp must be used inside <AppProvider>");
  return ctx;
}
