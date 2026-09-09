// ============================================================================
// MessageThread — the correspondence on a rights request.
//
// One component for both sides. The server decides direction from who is
// authenticated, so the only difference here is which side is drawn as "mine",
// and that comes from a prop rather than from anything the browser could assert
// to the API.
//
// Two deliberate choices about how this renders:
//
//   * Message bodies go through {} interpolation, never dangerouslySetInnerHTML.
//     Both directions are hostile input — a data principal's message is from a
//     member of the public, and a DPO's text ends up in an email — and React's
//     escaping is the reason a message containing <script> is a message
//     containing <script>.
//
//   * Attachments are links that trigger an authenticated fetch, not <a href>
//     pointing at the API. The download endpoints need a bearer token, and an
//     href would either 401 or require putting a credential in a URL.
// ============================================================================
import { useCallback, useEffect, useRef, useState } from "react";
import {
  downloadFile,
  humanSize,
  messages as loadMessages,
  saveBlob,
  sendMessage,
} from "../../api/fulfilment";

export default function MessageThread({
  requestId,
  /** "staff" or "principal" — which side to draw as mine. */
  side = "staff",
  /** Shown above the composer when the request is closed. */
  readOnly = false,
  onError,
}) {
  const [thread, setThread] = useState(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const bottom = useRef(null);

  const mine = side === "staff" ? "to_principal" : "from_principal";

  // The handler lives in a ref so `refresh` can reach the latest one without
  // taking it as a dependency. `onError` is a fresh closure on every parent
  // render, and depending on it would re-fetch the whole thread each time the
  // parent re-rendered for any unrelated reason.
  const onErrorRef = useRef(onError);
  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  const refresh = useCallback(async () => {
    try {
      const data = await loadMessages(requestId);
      setThread(data.messages || []);
    } catch (err) {
      setError(err.message);
      onErrorRef.current?.(err);
    }
  }, [requestId]);

  useEffect(() => {
    if (requestId) refresh();
  }, [requestId, refresh]);

  // Scroll the newest into view after a send, not on every render — jumping the
  // viewport while somebody is reading an older message is hostile.
  const scrollToEnd = () =>
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });

  const submit = async (e) => {
    e.preventDefault();
    const text = draft.trim();
    if (!text) return;
    setBusy(true);
    setError("");
    try {
      await sendMessage(requestId, text);
      setDraft("");
      await refresh();
      scrollToEnd();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const fetchAttachment = async (file) => {
    try {
      saveBlob(await downloadFile(file.id), file.filename);
    } catch (err) {
      setError(err.message);
    }
  };

  if (thread === null) {
    return <p className="text-sm text-muted">Loading the conversation…</p>;
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 space-y-3 overflow-y-auto pr-1">
        {thread.length === 0 && (
          <p className="text-sm text-muted">
            {side === "staff"
              ? "Nothing has been said yet. Anything you send here reaches the person who raised the request, and is kept with it."
              : "No messages yet. If we need anything from you, it will appear here."}
          </p>
        )}

        {thread.map((m) => {
          const isMine = m.direction === mine;
          return (
            <div
              key={m.id}
              className={`flex ${isMine ? "justify-end" : "justify-start"}`}
            >
              <div
                className={[
                  "max-w-[85%] rounded-lg px-3 py-2 text-sm",
                  isMine
                    ? "bg-teal/10 text-ink"
                    : "border border-line bg-canvas text-ink",
                ].join(" ")}
              >
                <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] text-muted">
                  <span className="font-medium">{m.author_label}</span>
                  {m.automated && (
                    <span className="rounded bg-line px-1.5 py-0.5 uppercase tracking-wide">
                      automatic
                    </span>
                  )}
                  <span>{new Date(m.created_at).toLocaleString()}</span>
                </div>

                {/* Escaped by React. Never innerHTML — see the header. */}
                <p className="whitespace-pre-wrap break-words">{m.body}</p>

                {(m.attachments || []).map((f) => (
                  <button
                    key={f.id}
                    type="button"
                    onClick={() => fetchAttachment(f)}
                    disabled={!f.available}
                    title={
                      f.available
                        ? `${f.filename} · sha256 ${f.sha256?.slice(0, 12)}…`
                        : `No longer available (${f.deleted_reason || "deleted"})`
                    }
                    className="mt-2 flex w-full items-center gap-2 rounded border border-line bg-surface px-2 py-1.5 text-left text-xs hover:bg-line/40 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <span aria-hidden="true">📎</span>
                    <span className="flex-1 truncate">{f.filename}</span>
                    <span className="text-muted">{humanSize(f.byte_size)}</span>
                  </button>
                ))}

                {isMine && side === "staff" && (
                  <p className="mt-1 text-[11px] text-muted">
                    {m.read_at
                      ? `Read ${new Date(m.read_at).toLocaleDateString()}`
                      : m.notified_at
                        ? "Sent — not read yet"
                        : "Sent"}
                  </p>
                )}
              </div>
            </div>
          );
        })}
        <div ref={bottom} />
      </div>

      {error && (
        <p className="mt-2 rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}

      {readOnly ? (
        <p className="mt-3 border-t border-line pt-3 text-sm text-muted">
          This request is closed, so the conversation is read-only. Raise a new
          request if you need something further.
        </p>
      ) : (
        <form onSubmit={submit} className="mt-3 border-t border-line pt-3">
          <label className="label" htmlFor="thread-message">
            {side === "staff" ? "Message the requester" : "Reply"}
          </label>
          <textarea
            id="thread-message"
            className="input min-h-[80px]"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            maxLength={20000}
            placeholder={
              side === "staff"
                ? "Ask a question, or explain a decision…"
                : "Anything you want to add…"
            }
          />
          <div className="mt-2 flex items-center justify-between">
            <p className="text-xs text-muted">
              {side === "staff"
                ? "They are emailed that a message is waiting. The text stays here — email is not a safe place for it."
                : "Kept with your request."}
            </p>
            <button
              type="submit"
              className="btn-primary"
              disabled={busy || !draft.trim()}
            >
              {busy ? "Sending…" : "Send"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
