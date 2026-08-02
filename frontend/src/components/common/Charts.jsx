// ============================================================================
// Charts — the three the admin dashboard needs, hand-rolled in SVG.
//
// No chart library: the brief's stack is React + Tailwind only, and these three
// shapes are simple enough that a dependency would cost more than it saves.
//
// Two rules applied throughout, because hue must never be the only channel:
//   • every series is DIRECTLY LABELLED with its value, and
//   • a legend with text labels is always present for multi-series charts.
// Marks stay thin, gridlines are hairline and recessive, and bars grow from a
// single baseline with rounded data-ends.
// ============================================================================

const PALETTE = {
  navy: "#1A3C5E",
  teal: "#0D7377",
  warning: "#F59E0B",
  success: "#22C55E",
  danger: "#EF4444",
  line: "#E2E8F0",
  muted: "#64748B",
};

function niceMax(value) {
  if (value <= 5) return 5;
  const pow = 10 ** Math.floor(Math.log10(value));
  return Math.ceil(value / pow) * pow;
}

// --------------------------------------------------------------- bar chart --
// DSAR requests by type. Three categories, each direct-labelled.
export function BarChart({ data, height = 200, colors = [PALETTE.navy, PALETTE.teal, PALETTE.warning] }) {
  const max = niceMax(Math.max(...data.map((d) => d.value), 1));
  const barW = 44;      // capped well under the band width; the rest is air
  const gap = 40;
  const padL = 36;
  const padB = 28;
  const padT = 18;
  const w = padL + data.length * (barW + gap);
  const h = height;
  const plotH = h - padB - padT;

  return (
    <figure className="m-0">
      <svg viewBox={`0 0 ${w} ${h}`} className="h-auto w-full" role="img"
           aria-label={`Bar chart: ${data.map((d) => `${d.label} ${d.value}`).join(", ")}`}>
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={padL} x2={w} y1={padT + plotH * (1 - f)} y2={padT + plotH * (1 - f)}
                  stroke={PALETTE.line} strokeWidth="1" />
            <text x={padL - 8} y={padT + plotH * (1 - f) + 4} textAnchor="end"
                  fontSize="10" fill={PALETTE.muted}>
              {Math.round(max * f)}
            </text>
          </g>
        ))}
        {data.map((d, i) => {
          const bh = Math.max((d.value / max) * plotH, 2);
          const x = padL + gap / 2 + i * (barW + gap);
          const y = padT + plotH - bh;
          return (
            <g key={d.label}>
              {/* 4px rounded data-end, square at the baseline */}
              <path
                d={`M${x},${padT + plotH} L${x},${y + 4} Q${x},${y} ${x + 4},${y}
                    L${x + barW - 4},${y} Q${x + barW},${y} ${x + barW},${y + 4}
                    L${x + barW},${padT + plotH} Z`}
                fill={colors[i % colors.length]}
              />
              <text x={x + barW / 2} y={y - 6} textAnchor="middle" fontSize="11"
                    fontWeight="600" fill={PALETTE.muted}>
                {d.value}
              </text>
              <text x={x + barW / 2} y={h - 8} textAnchor="middle" fontSize="11" fill={PALETTE.muted}>
                {d.label}
              </text>
            </g>
          );
        })}
      </svg>
    </figure>
  );
}

// -------------------------------------------------------------- line chart --
// Consents given vs withdrawn. Two series, both labelled at their end point.
export function LineChart({ data, series, height = 220 }) {
  const max = niceMax(Math.max(...data.flatMap((d) => series.map((s) => d[s.key])), 1));
  const padL = 40;
  const padR = 56;      // room for the end labels
  const padB = 26;
  const padT = 16;
  const w = 560;
  const plotW = w - padL - padR;
  const plotH = height - padT - padB;
  const x = (i) => padL + (plotW / Math.max(data.length - 1, 1)) * i;
  const y = (v) => padT + plotH - (v / max) * plotH;

  return (
    <figure className="m-0">
      <svg viewBox={`0 0 ${w} ${height}`} className="h-auto w-full" role="img"
           aria-label={`Line chart of ${series.map((s) => s.label).join(" and ")} over ${data.length} months`}>
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={padL} x2={padL + plotW} y1={padT + plotH * (1 - f)} y2={padT + plotH * (1 - f)}
                  stroke={PALETTE.line} strokeWidth="1" />
            <text x={padL - 8} y={padT + plotH * (1 - f) + 4} textAnchor="end" fontSize="10"
                  fill={PALETTE.muted}>
              {Math.round(max * f)}
            </text>
          </g>
        ))}
        {data.map((d, i) => (
          <text key={d.month} x={x(i)} y={height - 8} textAnchor="middle" fontSize="10"
                fill={PALETTE.muted}>
            {d.month}
          </text>
        ))}
        {series.map((s) => {
          const path = data.map((d, i) => `${i ? "L" : "M"}${x(i)},${y(d[s.key])}`).join(" ");
          const last = data[data.length - 1];
          return (
            <g key={s.key}>
              <path d={path} fill="none" stroke={s.color} strokeWidth="2"
                    strokeLinejoin="round" strokeLinecap="round" />
              {/* end marker with a 2px surface ring so it stays legible on crossings */}
              <circle cx={x(data.length - 1)} cy={y(last[s.key])} r="4.5" fill={s.color}
                      stroke="#FFFFFF" strokeWidth="2" />
              <text x={x(data.length - 1) + 10} y={y(last[s.key]) + 4} fontSize="11"
                    fontWeight="600" fill={PALETTE.muted}>
                {last[s.key]}
              </text>
            </g>
          );
        })}
      </svg>
      <figcaption className="mt-2 flex flex-wrap gap-4">
        {series.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-2 text-xs text-muted">
            <span className="h-0.5 w-4 rounded" style={{ background: s.color }} aria-hidden="true" />
            {s.label}
          </span>
        ))}
      </figcaption>
    </figure>
  );
}

// ------------------------------------------------------------- donut chart --
// Consent status distribution. Status semantics, so it uses the status palette;
// every slice is named with its value in the legend.
export function DonutChart({ data, size = 180, thickness = 26 }) {
  const total = data.reduce((a, d) => a + d.value, 0) || 1;
  const r = (size - thickness) / 2;
  const c = size / 2;
  const circ = 2 * Math.PI * r;
  const TONES = { success: PALETTE.success, danger: PALETTE.danger, warning: PALETTE.warning, info: PALETTE.navy };

  let offset = 0;
  return (
    <figure className="m-0 flex flex-wrap items-center gap-6">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img"
           aria-label={`Donut chart: ${data.map((d) => `${d.label} ${d.value}`).join(", ")}`}>
        <circle cx={c} cy={c} r={r} fill="none" stroke={PALETTE.line} strokeWidth={thickness} />
        {data.map((d) => {
          const len = (d.value / total) * circ;
          const el = (
            <circle
              key={d.label}
              cx={c} cy={c} r={r} fill="none"
              stroke={TONES[d.tone] || PALETTE.navy}
              strokeWidth={thickness}
              // 2px surface gap between segments, so neighbours read apart
              strokeDasharray={`${Math.max(len - 2, 0)} ${circ - Math.max(len - 2, 0)}`}
              strokeDashoffset={-offset}
              transform={`rotate(-90 ${c} ${c})`}
            />
          );
          offset += len;
          return el;
        })}
        <text x={c} y={c - 2} textAnchor="middle" fontSize="22" fontWeight="700" fill={PALETTE.navy}>
          {total}
        </text>
        <text x={c} y={c + 16} textAnchor="middle" fontSize="10" fill={PALETTE.muted}>
          consents
        </text>
      </svg>
      <figcaption className="space-y-2">
        {data.map((d) => (
          <div key={d.label} className="flex items-center gap-2 text-sm">
            <span className="h-2.5 w-2.5 rounded-full"
                  style={{ background: TONES[d.tone] || PALETTE.navy }} aria-hidden="true" />
            <span className="text-ink">{d.label}</span>
            <span className="font-semibold text-ink">{d.value}</span>
            <span className="text-xs text-muted">
              {Math.round((d.value / total) * 100)}%
            </span>
          </div>
        ))}
      </figcaption>
    </figure>
  );
}

export const CHART_COLORS = PALETTE;
