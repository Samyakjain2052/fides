// ============================================================================
// One assessment (/admin/assessments/:assessmentId)
//
// Answering it, assigning questions, and signing it off.
//
// PROGRESS COMES FROM THE SERVER. It evaluates the conditional questions before
// deciding what is outstanding, and this screen renders that answer rather than
// computing its own. Two implementations of `show_if` would eventually disagree,
// and the way that disagreement shows up is an assessment stuck at 90% that
// nobody can submit — at which point people stop using the tool.
//
// QUESTIONS ARE ASSIGNED INDIVIDUALLY. A DPIA asks about lawful basis (legal),
// retention (engineering), transfers (infrastructure) and vendor contracts
// (procurement). One assignee for the document means one person guessing at
// three other people's answers, which is how assessments become fiction.
//
// SIGN-OFF NEEDS A CONCLUSION. For a DPIA the conclusion is the output of the
// whole exercise. An assessment with every question answered and no stated
// finding has documented a process and decided nothing — which is the standing
// criticism of DPIAs as a genre, and the one thing a tool can actually prevent.
// ============================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  KIND_LABEL,
  STATUS_LABEL,
  approveAssessment,
  assessment as loadAssessment,
  assignQuestion,
  rejectAssessment,
  saveAnswer,
  submitAssessment,
  uploadEvidence,
} from "../../api/assessments";
import { listUsers } from "../../api/auth";
import { downloadFile, humanSize, saveBlob } from "../../api/fulfilment";
import { useApp } from "../../context/AppContext";

/** One question's control, by type. */
function AnswerControl({ question, answer, disabled, onSave }) {
  const [value, setValue] = useState(answer?.value ?? null);
  const [note, setNote] = useState(answer?.note ?? "");
  const [dirty, setDirty] = useState(false);

  // Re-seed when the server's copy changes (a reload, somebody else's edit).
  useEffect(() => {
    setValue(answer?.value ?? null);
    setNote(answer?.note ?? "");
    setDirty(false);
  }, [answer?.value, answer?.note]);

  const change = (next) => {
    setValue(next);
    setDirty(true);
  };

  const commit = () => {
    if (!dirty) return;
    onSave({ value, note });
    setDirty(false);
  };

  const common = { disabled, id: `q-${question.id}` };

  return (
    <div className="space-y-2">
      {question.type === "boolean" && (
        <div className="flex gap-4">
          {[
            [true, "Yes"],
            [false, "No"],
          ].map(([v, label]) => (
            <label key={label} className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name={`q-${question.id}`}
                checked={value === v}
                disabled={disabled}
                onChange={() => {
                  setValue(v);
                  setDirty(false);
                  onSave({ value: v, note });
                }}
              />
              {label}
            </label>
          ))}
        </div>
      )}

      {question.type === "single_choice" && (
        <select
          {...common}
          className="input"
          value={value ?? ""}
          onChange={(e) => {
            const v = e.target.value || null;
            setValue(v);
            setDirty(false);
            onSave({ value: v, note });
          }}
        >
          <option value="">Not answered</option>
          {question.options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      )}

      {question.type === "multi_choice" && (
        <div className="space-y-1">
          {question.options.map((o) => {
            const list = Array.isArray(value) ? value : [];
            return (
              <label key={o} className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={list.includes(o)}
                  disabled={disabled}
                  onChange={(e) => {
                    const next = e.target.checked
                      ? [...list, o]
                      : list.filter((x) => x !== o);
                    setValue(next);
                    setDirty(false);
                    onSave({ value: next, note });
                  }}
                />
                <span>{o}</span>
              </label>
            );
          })}
        </div>
      )}

      {question.type === "number" && (
        <input
          {...common}
          type="number"
          className="input max-w-[200px]"
          value={value ?? ""}
          onChange={(e) =>
            change(e.target.value === "" ? null : Number(e.target.value))
          }
          onBlur={commit}
        />
      )}

      {question.type === "date" && (
        <input
          {...common}
          type="date"
          className="input max-w-[220px]"
          value={typeof value === "string" ? value : ""}
          onChange={(e) => change(e.target.value || null)}
          onBlur={commit}
        />
      )}

      {question.type === "text" && (
        <input
          {...common}
          className="input"
          value={typeof value === "string" ? value : ""}
          onChange={(e) => change(e.target.value)}
          onBlur={commit}
        />
      )}

      {question.type === "long_text" && (
        <textarea
          {...common}
          className="input min-h-[90px]"
          value={typeof value === "string" ? value : ""}
          onChange={(e) => change(e.target.value)}
          onBlur={commit}
        />
      )}

      {/* Note is available on every type. An assessment answer of "Yes" is
          frequently "Yes, but only for the EU instance", and a form with
          nowhere to put the qualifier gets the bare "Yes". */}
      {question.type !== "evidence" && (
        <input
          className="input text-sm"
          placeholder="Anything to qualify that? (optional)"
          value={note}
          disabled={disabled}
          onChange={(e) => {
            setNote(e.target.value);
            setDirty(true);
          }}
          onBlur={() => {
            if (dirty) {
              onSave({ value, note });
              setDirty(false);
            }
          }}
        />
      )}
    </div>
  );
}

export default function AssessmentDetail() {
  const { assessmentId } = useParams();
  const { notify, user } = useApp();

  const [data, setData] = useState(null);
  const [people, setPeople] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [conclusion, setConclusion] = useState("");

  const load = useCallback(async () => {
    try {
      const fresh = await loadAssessment(assessmentId);
      setData(fresh);
      setConclusion(fresh.conclusion || "");
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }, [assessmentId]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    listUsers()
      .then((u) => setPeople(u.filter((p) => p.is_active)))
      .catch(() => setPeople([]));
  }, []);

  const run = async (fn, ok) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      if (ok) notify(ok);
      await load();
      return true;
    } catch (e) {
      setError(e.message);
      return false;
    } finally {
      setBusy(false);
    }
  };

  // Which questions actually apply, evaluated the same way the server does so
  // the screen and the progress bar agree.
  const visible = useMemo(() => {
    if (!data) return [];
    return data.questions.filter((q) => {
      if (!q.show_if) return true;
      const dep = data.answers[q.show_if.question];
      if (!dep) return false;
      return JSON.stringify(dep.value) === JSON.stringify(q.show_if.equals);
    });
  }, [data]);

  const sections = useMemo(() => {
    const out = new Map();
    for (const q of visible) {
      const key = q.section || "";
      if (!out.has(key)) out.set(key, []);
      out.get(key).push(q);
    }
    return [...out.entries()];
  }, [visible]);

  if (error && !data) {
    return (
      <div className="space-y-4">
        <Link to="/admin/assessments" className="text-sm text-teal underline">
          ← Back to assessments
        </Link>
        <p className="rounded-lg border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">
          {error}
        </p>
      </div>
    );
  }
  if (!data) return <p className="text-sm text-muted">Loading…</p>;

  const locked = data.status === "approved" || data.status === "archived";
  const outstanding = new Set(
    (data.progress.outstanding || []).map((o) => o.question_id),
  );

  return (
    <div className="space-y-5">
      {/* -------------------------------------------------------- header -- */}
      <header className="space-y-1">
        <Link to="/admin/assessments" className="text-sm text-teal underline">
          ← Back to assessments
        </Link>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold text-ink">{data.name}</h1>
          <span className="rounded-full border border-line bg-surface px-2 py-0.5 text-xs uppercase tracking-wide text-muted">
            {KIND_LABEL[data.kind] || data.kind}
          </span>
          <span className="rounded-full border border-line bg-surface px-2 py-0.5 text-xs text-muted">
            {STATUS_LABEL[data.status] || data.status}
          </span>
          {data.overdue && (
            <span className="rounded-full bg-danger px-2 py-0.5 text-xs font-semibold text-white">
              overdue
            </span>
          )}
        </div>
        <p className="text-sm text-muted">
          {data.template}
          {data.subject_label && <> · about {data.subject_label}</>}
          {data.manager_label && <> · {data.manager_label} is accountable</>}
          {data.due_at && (
            <> · due {new Date(data.due_at).toLocaleDateString()}</>
          )}
        </p>
      </header>

      {/* ------------------------------------------------------ progress -- */}
      <div className="card p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-sm font-semibold text-ink">
              {data.progress.answered} of {data.progress.questions} answered
            </p>
            {data.progress.hidden > 0 && (
              <p className="text-xs text-muted">
                {data.progress.hidden} question(s) do not apply, based on the
                answers so far.
              </p>
            )}
          </div>
          <p className="text-2xl font-semibold text-ink">
            {data.progress.percent}%
          </p>
        </div>
        <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-line">
          <div
            className="h-full rounded-full bg-teal transition-all"
            style={{ width: `${data.progress.percent}%` }}
          />
        </div>

        {data.progress.outstanding.length > 0 && (
          <div className="mt-3 rounded-lg border border-warning/40 bg-warning/10 p-3">
            <p className="text-sm font-medium text-ink">
              Still needed ({data.progress.outstanding.length})
            </p>
            <ul className="mt-1 space-y-1 text-sm text-muted">
              {data.progress.outstanding.slice(0, 6).map((o) => (
                <li key={o.question_id}>
                  · {o.prompt}
                  {o.assignee_user_id && (
                    <span className="text-xs"> — assigned</span>
                  )}
                </li>
              ))}
              {data.progress.outstanding.length > 6 && (
                <li className="text-xs">
                  …and {data.progress.outstanding.length - 6} more
                </li>
              )}
            </ul>
          </div>
        )}
      </div>

      {error && (
        <p className="rounded-lg border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-danger">
          {error}
        </p>
      )}

      {data.rejection_reason && (
        <p className="rounded-lg border border-danger/40 bg-danger/5 px-4 py-3 text-sm text-ink">
          <strong className="font-semibold">Sent back for rework:</strong>{" "}
          {data.rejection_reason}
        </p>
      )}

      {/* ----------------------------------------------------- questions -- */}
      {sections.map(([section, questions]) => (
        <section key={section || "general"} className="card overflow-hidden">
          {section && (
            <h2 className="border-b border-line px-5 py-3 font-semibold text-ink">
              {section}
            </h2>
          )}
          <div className="divide-y divide-line">
            {questions.map((q) => {
              const answer = data.answers[q.id];
              const files = data.evidence[q.id] || [];
              return (
                <div key={q.id} className="space-y-2 p-5">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <label
                        className="block font-medium text-ink"
                        htmlFor={`q-${q.id}`}
                      >
                        {q.prompt}
                        {q.required && (
                          <span
                            className="ml-1 text-danger"
                            title="Required before this can be submitted"
                          >
                            *
                          </span>
                        )}
                      </label>
                      {q.helper_text && (
                        <p className="mt-1 text-sm text-muted">
                          {q.helper_text}
                        </p>
                      )}
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      {outstanding.has(q.id) && (
                        <span className="rounded-full border border-warning/50 bg-warning/10 px-2 py-0.5 text-[11px] text-ink">
                          needed
                        </span>
                      )}
                      {!locked && (
                        <select
                          className="input max-w-[180px] py-1 text-sm"
                          value={answer?.assignee_user_id || ""}
                          onChange={(e) =>
                            run(() =>
                              assignQuestion(
                                assessmentId,
                                q.id,
                                e.target.value || null,
                              ),
                            )
                          }
                          title="Who should answer this one"
                        >
                          <option value="">Anyone</option>
                          {people.map((p) => (
                            <option key={p.id} value={p.id}>
                              {p.full_name || p.email}
                            </option>
                          ))}
                        </select>
                      )}
                    </div>
                  </div>

                  {q.type === "evidence" ? (
                    <div className="space-y-2">
                      {files.map((f) => (
                        <button
                          key={f.id}
                          type="button"
                          onClick={async () => {
                            try {
                              saveBlob(await downloadFile(f.id), f.filename);
                            } catch (e) {
                              setError(e.message);
                            }
                          }}
                          className="flex w-full items-center gap-2 rounded border border-line bg-canvas px-2 py-1.5 text-left text-xs hover:bg-line/40"
                        >
                          <span aria-hidden="true">📎</span>
                          <span className="flex-1 truncate">{f.filename}</span>
                          <span className="text-muted">
                            {humanSize(f.byte_size)}
                          </span>
                        </button>
                      ))}
                      {!locked && (
                        <label className="btn-secondary inline-flex cursor-pointer items-center text-sm">
                          Attach a document
                          <input
                            type="file"
                            className="hidden"
                            onChange={async (e) => {
                              const file = e.target.files?.[0];
                              if (!file) return;
                              await run(
                                () =>
                                  uploadEvidence(assessmentId, q.id, file),
                                "Attached.",
                              );
                              e.target.value = "";
                            }}
                          />
                        </label>
                      )}
                    </div>
                  ) : (
                    <AnswerControl
                      question={q}
                      answer={answer}
                      disabled={locked || busy}
                      onSave={({ value, note }) =>
                        run(() =>
                          saveAnswer(assessmentId, q.id, { value, note }),
                        )
                      }
                    />
                  )}

                  {answer?.answered_at && (
                    <p className="text-xs text-muted">
                      answered{" "}
                      {new Date(answer.answered_at).toLocaleDateString()}
                      {answer.assignee_label && ` · ${answer.assignee_label}`}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      ))}

      {/* -------------------------------------------------------- sign-off -- */}
      <section className="card p-5">
        <h2 className="font-semibold text-ink">Sign-off</h2>

        {data.status === "approved" ? (
          <div className="mt-2 space-y-2">
            <p className="rounded-lg border border-success/40 bg-success/5 px-3 py-2 text-sm text-ink">
              <strong className="font-semibold">
                Approved{" "}
                {data.approved_at &&
                  new Date(data.approved_at).toLocaleString()}
              </strong>
              <br />
              {data.conclusion}
            </p>
            <p className="text-xs text-muted">
              The answers are fixed. Start a new assessment if the position has
              changed — the value of this record is that it says what was known
              and decided at the time.
              {data.next_review_at && (
                <>
                  {" "}
                  Next review{" "}
                  {new Date(data.next_review_at).toLocaleDateString()}.
                </>
              )}
            </p>
          </div>
        ) : (
          <div className="mt-2 space-y-3">
            <div>
              <label className="label" htmlFor="conclusion">
                Conclusion
              </label>
              <textarea
                id="conclusion"
                className="input min-h-[90px]"
                value={conclusion}
                onChange={(e) => setConclusion(e.target.value)}
                placeholder={
                  data.kind === "dpia"
                    ? "What is the residual risk, and should the processing proceed?"
                    : "What did you conclude?"
                }
              />
              <p className="mt-1 text-xs text-muted">
                Required to approve.{" "}
                {data.kind === "dpia"
                  ? "For a DPIA this is the output of the exercise — every question answered and no stated finding has documented a process and decided nothing."
                  : "An assessment with no conclusion has recorded answers and decided nothing."}
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              {data.status !== "in_review" && (
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={busy || !data.progress.complete}
                  title={
                    data.progress.complete
                      ? undefined
                      : "Required questions are still unanswered"
                  }
                  onClick={() =>
                    run(
                      () => submitAssessment(assessmentId),
                      "Sent for sign-off.",
                    )
                  }
                >
                  Send for sign-off
                </button>
              )}
              <button
                type="button"
                className="btn-primary"
                disabled={
                  busy || !data.progress.complete || !conclusion.trim()
                }
                onClick={() =>
                  run(
                    () => approveAssessment(assessmentId, conclusion.trim()),
                    "Approved.",
                  )
                }
              >
                Approve
              </button>
              {data.status === "in_review" && (
                <button
                  type="button"
                  className="btn-ghost text-danger"
                  disabled={busy}
                  onClick={() => {
                    const reason = window.prompt(
                      "What needs to change? A rejection with no reason sends somebody back to a document with no idea what to fix.",
                    );
                    if (reason?.trim())
                      run(
                        () => rejectAssessment(assessmentId, reason.trim()),
                        "Sent back.",
                      );
                  }}
                >
                  Send back for rework
                </button>
              )}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
