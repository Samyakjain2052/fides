// ============================================================================
// Questionnaires (/admin/assessments/templates)
//
// The templates assessments are run from, and the builder for them.
//
// PUBLISHING IS THE POINT OF NO RETURN, and the screen says so before it
// happens. A published version's questions are what people were asked, so they
// stop being editable — and "editing" one afterwards means creating the next
// version, which leaves every running assessment pinned to the version it
// started on. The alternative is a questionnaire that can be rewritten under
// its own answers, which makes "we assessed this in March" unfalsifiable.
// ============================================================================
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  KIND_LABEL,
  addQuestion,
  createTemplate,
  deleteQuestion,
  newVersion,
  publishTemplate,
  seedTemplates,
  template as loadTemplate,
  templates as loadTemplates,
} from "../../api/assessments";
import { useApp } from "../../context/AppContext";

const TYPES = [
  ["long_text", "Long text"],
  ["text", "Short text"],
  ["boolean", "Yes / no"],
  ["single_choice", "Choose one"],
  ["multi_choice", "Choose several"],
  ["number", "Number"],
  ["date", "Date"],
  ["evidence", "Upload a document"],
];

export default function AssessmentTemplates() {
  const { notify } = useApp();

  const [rows, setRows] = useState([]);
  const [open, setOpen] = useState(null); // the expanded template's detail
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);

  const [form, setForm] = useState({ slug: "", kind: "custom", name: "" });
  const [q, setQ] = useState({
    prompt: "",
    type: "long_text",
    section: "",
    helperText: "",
    required: false,
    options: "",
    reportable: false,
  });

  const load = useCallback(async () => {
    try {
      setRows(await loadTemplates());
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const expand = async (id) => {
    if (open?.id === id) {
      setOpen(null);
      return;
    }
    try {
      setOpen(await loadTemplate(id));
    } catch (e) {
      setError(e.message);
    }
  };

  const run = async (fn, ok) => {
    setBusy(true);
    setError("");
    try {
      const result = await fn();
      if (ok) notify(ok);
      await load();
      if (open) setOpen(await loadTemplate(open.id));
      return result;
    } catch (e) {
      setError(e.message);
      return null;
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link to="/admin/assessments" className="text-sm text-teal underline">
            ← Back to assessments
          </Link>
          <h1 className="mt-1 text-xl font-semibold text-ink">Questionnaires</h1>
          <p className="text-sm text-muted">
            The templates assessments run from. Published versions are frozen,
            because their questions are what people were asked.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn-secondary"
            disabled={busy}
            onClick={() =>
              run(seedTemplates, "Standard questionnaires installed.")
            }
          >
            Install the standard set
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={() => setCreating((v) => !v)}
          >
            {creating ? "Cancel" : "New questionnaire"}
          </button>
        </div>
      </header>

      {error && (
        <p className="rounded-lg border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">
          {error}
        </p>
      )}

      {creating && (
        <form
          className="card grid gap-4 p-5 sm:grid-cols-3"
          onSubmit={async (e) => {
            e.preventDefault();
            const ok = await run(
              () =>
                createTemplate({
                  slug: form.slug.trim(),
                  kind: form.kind,
                  name: form.name.trim(),
                }),
              "Draft created. Add questions, then publish it.",
            );
            if (ok) {
              setCreating(false);
              setForm({ slug: "", kind: "custom", name: "" });
            }
          }}
        >
          <div>
            <label className="label" htmlFor="t-name">
              Name
            </label>
            <input
              id="t-name"
              className="input"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              required
            />
          </div>
          <div>
            <label className="label" htmlFor="t-slug">
              Short identifier
            </label>
            <input
              id="t-slug"
              className="input"
              value={form.slug}
              onChange={(e) => setForm({ ...form, slug: e.target.value })}
              placeholder="vendor-lite"
              required
            />
            <p className="mt-1 text-xs text-muted">
              Stays the same across versions.
            </p>
          </div>
          <div>
            <label className="label" htmlFor="t-kind">
              Kind
            </label>
            <select
              id="t-kind"
              className="input"
              value={form.kind}
              onChange={(e) => setForm({ ...form, kind: e.target.value })}
            >
              {Object.entries(KIND_LABEL).map(([id, label]) => (
                <option key={id} value={id}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <div className="sm:col-span-3">
            <button type="submit" className="btn-primary" disabled={busy}>
              Create draft
            </button>
          </div>
        </form>
      )}

      <ul className="space-y-3">
        {rows.map((t) => (
          <li key={t.id} className="card overflow-hidden">
            <div className="flex flex-wrap items-start justify-between gap-3 p-4">
              <div>
                <button
                  type="button"
                  className="font-medium text-ink hover:underline"
                  onClick={() => expand(t.id)}
                >
                  {t.name}
                </button>
                <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                  <span>v{t.version}</span>
                  <span>· {KIND_LABEL[t.kind] || t.kind}</span>
                  {t.published ? (
                    <span className="rounded-full border border-success/50 bg-success/10 px-2 py-0.5 text-ink">
                      published — frozen
                    </span>
                  ) : (
                    <span className="rounded-full border border-warning/50 bg-warning/10 px-2 py-0.5 text-ink">
                      draft — editable
                    </span>
                  )}
                  {t.built_in && <span>· shipped with the product</span>}
                </p>
                {t.description && (
                  <p className="mt-1 max-w-2xl text-sm text-muted">
                    {t.description}
                  </p>
                )}
              </div>
              <div className="flex shrink-0 flex-wrap gap-2">
                <button
                  type="button"
                  className="btn-secondary text-sm"
                  onClick={() => expand(t.id)}
                >
                  {open?.id === t.id ? "Hide questions" : "Questions"}
                </button>
                {t.published ? (
                  <button
                    type="button"
                    className="btn-secondary text-sm"
                    disabled={busy}
                    onClick={() =>
                      run(
                        () => newVersion(t.id),
                        `Created v${t.version + 1} as a draft. Running assessments stay on v${t.version}.`,
                      )
                    }
                  >
                    Edit as new version
                  </button>
                ) : (
                  <button
                    type="button"
                    className="btn-primary text-sm"
                    disabled={busy}
                    onClick={() => {
                      if (
                        window.confirm(
                          "Publishing freezes this version's questions — they become what people were asked, and cannot be changed. Editing later creates v" +
                            (t.version + 1) +
                            " and leaves running assessments on this one. Publish?",
                        )
                      )
                        run(() => publishTemplate(t.id), "Published.");
                    }}
                  >
                    Publish
                  </button>
                )}
              </div>
            </div>

            {/* ------------------------------------------------ questions -- */}
            {open?.id === t.id && (
              <div className="border-t border-line bg-canvas p-4">
                <ol className="space-y-2">
                  {(open.questions || []).map((question) => (
                    <li
                      key={question.id}
                      className="flex flex-wrap items-start justify-between gap-2 rounded-lg border border-line bg-surface p-3"
                    >
                      <div className="min-w-0 flex-1">
                        {question.section && (
                          <p className="text-[11px] uppercase tracking-wide text-muted">
                            {question.section}
                          </p>
                        )}
                        <p className="text-sm text-ink">
                          {question.prompt}
                          {question.required && (
                            <span className="text-danger"> *</span>
                          )}
                        </p>
                        {question.helper_text && (
                          <p className="mt-1 text-xs text-muted">
                            {question.helper_text}
                          </p>
                        )}
                        <p className="mt-1 flex flex-wrap gap-2 text-[11px] text-muted">
                          <span>
                            {TYPES.find(([id]) => id === question.type)?.[1] ||
                              question.type}
                          </span>
                          {question.options.length > 0 && (
                            <span>· {question.options.length} options</span>
                          )}
                          {question.show_if && <span>· conditional</span>}
                          {question.reportable && <span>· reportable</span>}
                        </p>
                      </div>
                      {!t.published && (
                        <button
                          type="button"
                          className="btn-ghost text-xs text-danger"
                          disabled={busy}
                          onClick={() =>
                            run(
                              () => deleteQuestion(t.id, question.id),
                              "Removed.",
                            )
                          }
                        >
                          Remove
                        </button>
                      )}
                    </li>
                  ))}
                  {(open.questions || []).length === 0 && (
                    <li className="text-sm text-muted">
                      No questions yet. A template with none would produce
                      assessments that are complete the moment they are created,
                      so it cannot be published until it has at least one.
                    </li>
                  )}
                </ol>

                {!t.published && (
                  <form
                    className="mt-4 grid gap-3 border-t border-line pt-4 sm:grid-cols-2"
                    onSubmit={async (e) => {
                      e.preventDefault();
                      const ok = await run(
                        () =>
                          addQuestion(t.id, {
                            prompt: q.prompt.trim(),
                            type: q.type,
                            section: q.section.trim(),
                            helperText: q.helperText.trim(),
                            required: q.required,
                            reportable: q.reportable,
                            options: q.options
                              .split("\n")
                              .map((o) => o.trim())
                              .filter(Boolean),
                          }),
                        "Question added.",
                      );
                      if (ok)
                        setQ({
                          prompt: "",
                          type: "long_text",
                          section: q.section,
                          helperText: "",
                          required: false,
                          options: "",
                          reportable: false,
                        });
                    }}
                  >
                    <div className="sm:col-span-2">
                      <label className="label" htmlFor={`q-prompt-${t.id}`}>
                        The question
                      </label>
                      <input
                        id={`q-prompt-${t.id}`}
                        className="input"
                        value={q.prompt}
                        onChange={(e) => setQ({ ...q, prompt: e.target.value })}
                        required
                      />
                    </div>
                    <div>
                      <label className="label" htmlFor={`q-type-${t.id}`}>
                        Answered how
                      </label>
                      <select
                        id={`q-type-${t.id}`}
                        className="input"
                        value={q.type}
                        onChange={(e) => setQ({ ...q, type: e.target.value })}
                      >
                        {TYPES.map(([id, label]) => (
                          <option key={id} value={id}>
                            {label}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label className="label" htmlFor={`q-section-${t.id}`}>
                        Section (optional)
                      </label>
                      <input
                        id={`q-section-${t.id}`}
                        className="input"
                        value={q.section}
                        onChange={(e) =>
                          setQ({ ...q, section: e.target.value })
                        }
                        placeholder="Describe the processing"
                      />
                    </div>
                    <div className="sm:col-span-2">
                      <label className="label" htmlFor={`q-helper-${t.id}`}>
                        Guidance (optional, but worth writing)
                      </label>
                      <textarea
                        id={`q-helper-${t.id}`}
                        className="input min-h-[60px]"
                        value={q.helperText}
                        onChange={(e) =>
                          setQ({ ...q, helperText: e.target.value })
                        }
                        placeholder="What should somebody think about before answering?"
                      />
                      <p className="mt-1 text-xs text-muted">
                        A question with no guidance gets a one-line answer. This
                        is where a template earns its keep.
                      </p>
                    </div>
                    {["single_choice", "multi_choice"].includes(q.type) && (
                      <div className="sm:col-span-2">
                        <label className="label" htmlFor={`q-options-${t.id}`}>
                          Options, one per line
                        </label>
                        <textarea
                          id={`q-options-${t.id}`}
                          className="input min-h-[70px]"
                          value={q.options}
                          onChange={(e) =>
                            setQ({ ...q, options: e.target.value })
                          }
                          required
                        />
                      </div>
                    )}
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={q.required}
                        onChange={(e) =>
                          setQ({ ...q, required: e.target.checked })
                        }
                      />
                      Must be answered before sign-off
                    </label>
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={q.reportable}
                        onChange={(e) =>
                          setQ({ ...q, reportable: e.target.checked })
                        }
                      />
                      Include in exports and reports
                    </label>
                    <div className="sm:col-span-2">
                      <button
                        type="submit"
                        className="btn-secondary"
                        disabled={busy || !q.prompt.trim()}
                      >
                        Add question
                      </button>
                    </div>
                  </form>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
