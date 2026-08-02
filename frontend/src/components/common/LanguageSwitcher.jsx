// ============================================================================
// LanguageSwitcher — English + the 22 Eighth Schedule languages.
// Must be visible on every user-facing screen (consent, DSAR, grievance,
// cookie). Selection is stored in localStorage via AppContext.
// ============================================================================
import { useApp } from "../../context/AppContext";

export default function LanguageSwitcher({ compact = false }) {
  const { language, setLanguage, languages, translated } = useApp();

  return (
    <div className="flex items-center gap-2">
      <label htmlFor="language" className="sr-only">
        Choose your language
      </label>
      {!compact && (
        <span className="text-sm text-muted" aria-hidden="true">
          🌐
        </span>
      )}
      <select
        id="language"
        className="input w-auto py-1.5 text-sm"
        value={language}
        onChange={(e) => setLanguage(e.target.value)}
      >
        {languages.map((l) => (
          <option key={l} value={l}>
            {l}
          </option>
        ))}
      </select>
      {/* Honest about the state of translation rather than pretending: the real
          translation service (Bhashini) is explicitly out of scope for now. */}
      {!translated && (
        <span className="tag" title="Translation service not connected yet — showing English">
          English fallback
        </span>
      )}
    </div>
  );
}
