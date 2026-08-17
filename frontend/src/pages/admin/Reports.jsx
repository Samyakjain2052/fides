// ============================================================================
// Reports (/admin/reports)
//
// Real reports, generated from real data.
//
// What the previous version claimed and this one does not: the audit report
// "carries a digital signature for the regulator". It did not. Nothing here is
// labelled signed, and the provenance block says in as many words that the chain
// head hash is tamper evidence rather than a signature.
//
// Other removals: the PDF format (the server produces CSV and JSON — a dead PDF
// button makes a customer plan around a capability that does not exist), and the
// list of previously-generated reports, which was a mock table of files that were
// never created. Reports are streamed and never stored, so there is nothing to
// list; the audit trail records who generated what.
//
// Empty states are first-class. "No consent activity in this period" is a real
// answer and renders as one — not as an empty axis that reads as a broken chart.
// ============================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  catalogue,
  download,
  FORMATS,
  preview,
  RANGES,
} from "../../api/reports";
import { useApp } from "../../context/AppContext";

/** A truncated cell keeps the table readable; the export has the full value. */
function Cell({ value }) {
  if (value === null || value === undefined || value === "") {
    return <span className="text-muted">—</span>;
  }
  if (typeof value === "boolean") {
    return <span className={value ? "text-success" : "text-muted"}>{String(value)}</span>;
  }
  const text = String(value);
  const looksIso = /^\d{4}-\d{2}-\d{2}T/.test(text);
  const shown = looksIso ? new Date(text).toLocaleString() : text;
  return (
    <span title={shown.length > 28 ? shown : undefined}>
      {shown.length > 28 ? `${shown.slice(0, 27)}…` : shown}
    </span>
  );
}

export default function Reports() {
  const { notify } = useApp();

  const [cat, setCat] = useState(null);
  const [active, setActive] = useState(null);
  const [rangeId, setRangeId] = useState("30d");
  const [format, setFormat] = useState("csv");
  const [verifyChain, setVerifyChain] = useState(false);
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    catalogue()
      .then((c) => {
        setCat(c);
        setActive(c.reports[0]?.key ?? null);
      })
      .catch((e) => setError(e.message));
  }, []);

  const load = useCallback(async () => {
    if (!active) return;
    setLoading(true);
    setError("");
    try {
      setData(await preview(active, { rangeId, verifyChain }));
    } catch (e) {
      setError(e.message);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [active, rangeId, verifyChain]);

  useEffect(() => {
    load();
  }, [load]);

  const definition = useMemo(
    () => cat?.reports.find((r) => r.key === active) ?? null,
    [cat, active],
  );

  const onDownload = async () => {
    setBusy(true);
    setError("");
    try {
      const filename = await download(active, { rangeId, format, verifyChain });
      notify(`${filename} downloaded.`);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  if (!cat) {
    return (
      <p className="text-sm text-muted">
        {error || "Loading the report catalogue…"}
      </p>
    );
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">Compliance reports</h1>
        <p className="text-sm text-muted">
          Generated from live data at the moment you ask. Nothing is stored, so
          nothing can drift from the records it describes.
        </p>
      </div>

      {/* ------------------------------------------------- what this is not -- */}
      <div className="rounded-lg border border-line bg-canvas p-4 text-xs text-muted">
        <p className="font-semibold text-ink">These reports are not signed.</p>
        <p className="mt-1">{cat.signing}</p>
        <p className="mt-2">
          Formats: {cat.formats.join(", ").toUpperCase()}. Periods are capped at{" "}
          {cat.max_period_days} days and exports at{" "}
          {cat.max_rows.toLocaleString()} rows — a report that hits either cap
          says so in its provenance block.
        </p>
      </div>

      {error && (
        <div className="rounded-lg border border-danger/50 bg-danger/10 p-3 text-sm text-ink">
          {error}
        </div>
      )}

      {/* ---------------------------------------------------- report picker -- */}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {cat.reports.map((r) => (
          <button
            key={r.key}
            type="button"
            onClick={() => setActive(r.key)}
            className={`card p-4 text-left transition ${
              active === r.key ? "border-teal ring-1 ring-teal/40" : "hover:bg-line/20"
            }`}
          >
            <p className="font-semibold text-ink">{r.title}</p>
            <p className="mt-1 text-xs text-muted">{r.question}</p>
          </button>
        ))}
      </div>

      {/* --------------------------------------------------------- controls -- */}
      <div className="card flex flex-wrap items-end gap-3 p-4">
        <div>
          <label className="label" htmlFor="r-range">Period</label>
          <select
            id="r-range"
            className="input"
            value={rangeId}
            onChange={(e) => setRangeId(e.target.value)}
          >
            {RANGES.map((r) => (
              <option key={r.id} value={r.id}>{r.label}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="r-format">Format</label>
          <select
            id="r-format"
            className="input"
            value={format}
            onChange={(e) => setFormat(e.target.value)}
          >
            {FORMATS.map((f) => (
              <option key={f.id} value={f.id}>{f.label}</option>
            ))}
          </select>
        </div>
        <label className="flex items-center gap-2 pb-2 text-sm text-ink">
          <input
            type="checkbox"
            checked={verifyChain}
            onChange={(e) => setVerifyChain(e.target.checked)}
          />
          <span title="Recomputes every entry in the audit chain. Slower, and off by default so the report does not claim a check nobody ran.">
            Verify the audit chain
          </span>
        </label>
        <button
          type="button"
          className="btn-primary ml-auto"
          onClick={onDownload}
          disabled={busy || !active}
        >
          {busy ? "Generating…" : `Download ${format.toUpperCase()}`}
        </button>
      </div>

      {definition?.caveats?.length > 0 && (
        <ul className="space-y-1.5 text-xs text-muted">
          {definition.caveats.map((c, i) => (
            <li key={i} className="rounded-lg border border-warning/40 bg-warning/5 px-3 py-2">
              {c}
            </li>
          ))}
        </ul>
      )}

      {/* ---------------------------------------------------------- preview -- */}
      <section className="card overflow-hidden">
        <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-line px-5 py-3">
          <div>
            <h2 className="font-semibold text-ink">{definition?.title}</h2>
            {definition?.notes && (
              <p className="text-xs text-muted">{definition.notes}</p>
            )}
          </div>
          {data && (
            <p className="text-xs text-muted">
              Showing {data.rows.length} of {data.total.toLocaleString()} matching
              row{data.total === 1 ? "" : "s"}
            </p>
          )}
        </div>

        {loading ? (
          <p className="px-5 py-8 text-center text-sm text-muted">Running the query…</p>
        ) : !data ? (
          <p className="px-5 py-8 text-center text-sm text-muted">
            Nothing to show.
          </p>
        ) : data.rows.length === 0 ? (
          // An empty report is a legitimate report. It says so in words rather
          // than rendering an empty table that reads as a failure.
          <div className="px-5 py-10 text-center">
            <p className="text-sm font-medium text-ink">
              No {definition.title.toLowerCase()} activity in this period.
            </p>
            <p className="mx-auto mt-1 max-w-md text-xs text-muted">
              That is a real answer, not a loading state. The export below will
              contain the same finding, with its provenance block, so it can be
              filed as evidence that there was nothing to report.
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-line">
              <thead className="bg-canvas">
                <tr>
                  {data.columns.map((c) => (
                    <th key={c} className="th whitespace-nowrap">
                      {c.replace(/_/g, " ")}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {data.rows.map((row, i) => (
                  <tr key={i}>
                    {data.columns.map((c) => (
                      <td key={c} className="td whitespace-nowrap text-xs">
                        <Cell value={row[c]} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ------------------------------------------------------ provenance -- */}
      {data && (
        <section className="card p-5">
          <h2 className="font-semibold text-ink">Provenance</h2>
          <p className="text-xs text-muted">
            The same block the export carries, verbatim. Whoever reads the file is
            making the same decisions as whoever reads this screen.
          </p>
          {data.provenance.truncated && (
            <p className="mt-3 rounded-lg border border-warning/50 bg-warning/10 px-3 py-2 text-sm text-ink">
              This report would be truncated: {data.provenance.total_matching.toLocaleString()}{" "}
              rows match and the export is capped at{" "}
              {data.provenance.row_limit.toLocaleString()}. Narrow the period to
              get the rest.
            </p>
          )}
          <pre className="mt-3 overflow-x-auto rounded-lg border border-line bg-canvas p-3 text-[11px] leading-relaxed text-ink">
            {data.provenance_lines.join("\n")}
          </pre>
          <p className="mt-2 text-xs text-muted">
            Chain verification is{" "}
            <strong className="text-ink">
              {data.provenance.chain_verified === "ok"
                ? "OK"
                : data.provenance.chain_verified === "failed"
                  ? "FAILED"
                  : "not checked"}
            </strong>
            {data.provenance.chain_verified === "not_checked" &&
              " — tick “Verify the audit chain” above to check it. Left unchecked, the report states that rather than implying a pass."}
            {data.provenance.chain_problem && ` — ${data.provenance.chain_problem}`}
          </p>
        </section>
      )}
    </div>
  );
}
