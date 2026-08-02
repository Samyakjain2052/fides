// ============================================================================
// The honesty layer. Anything reading sample data says so, in the same place,
// in the same words, every time.
//
// Deliberately not dismissible. A banner a buyer can close is a banner they
// close on the first screen and never see again — and then every later screen
// is quietly making a claim we cannot support.
// ============================================================================
import { Link } from "react-router-dom";
import { MODULE_CAVEATS, MODULE_LABELS, SHIP_TARGET, isPreview } from "../../config/modules";

/**
 * Full-width banner for a preview module. Renders nothing for a live module, so
 * screens can mount it unconditionally and it disappears on its own when the
 * module ships.
 */
export default function PreviewBanner({ module }) {
  if (!isPreview(module)) return <LiveCaveat module={module} />;

  return (
    <div
      role="status"
      className="rounded-lg border border-warning/50 bg-warning/10 px-4 py-3"
    >
      <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
        <span className="rounded-full bg-warning px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-white">
          Preview
        </span>
        <p className="min-w-0 flex-1 text-sm text-ink">
          <strong className="font-semibold">
            {MODULE_LABELS[module] || "This module"} is not live yet.
          </strong>{" "}
          Everything on this screen is sample data, and the controls are
          disabled. Shipping {SHIP_TARGET}.{" "}
          <Link to="/roadmap" className="text-teal underline">
            See what is live today
          </Link>
        </p>
      </div>
    </div>
  );
}

/**
 * A live module can still have an exception. Saying "DSAR is live" while the
 * correction tab quietly serves sample rows is the same mis-sell in miniature,
 * so caveats get their own (quieter) banner.
 */
function LiveCaveat({ module }) {
  const caveat = MODULE_CAVEATS[module];
  if (!caveat) return null;

  return (
    <div role="status" className="rounded-lg border border-info/40 bg-info/10 px-4 py-3">
      <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
        <span className="rounded-full bg-info px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-white">
          Partly live
        </span>
        <p className="min-w-0 flex-1 text-sm text-ink">
          {caveat}{" "}
          <Link to="/roadmap" className="text-teal underline">
            Full status
          </Link>
        </p>
      </div>
    </div>
  );
}

/** Small inline badge for nav items and list rows. */
export function PreviewBadge({ module, className = "" }) {
  // No module at all is not the same as an unknown module. `isPreview` fails
  // closed for unrecognised keys — right for a typo, wrong for a nav item like
  // "Dashboard" that spans several modules and carries its own banner.
  if (!module) return null;
  if (!isPreview(module)) return null;
  return (
    <span
      title={`Preview — sample data. Shipping ${SHIP_TARGET}.`}
      className={
        "rounded-full border border-warning/60 bg-warning/15 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-warning " +
        className
      }
    >
      Preview
    </span>
  );
}

/**
 * Marks a single number as illustrative. Used where a figure would otherwise
 * read as a real measurement — which on a compliance dashboard is the most
 * damaging thing on the page.
 */
export function SampleTag({ className = "" }) {
  return (
    <span
      title="Sample figure — not a measurement of real activity."
      className={
        "rounded border border-line bg-canvas px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-muted " +
        className
      }
    >
      Sample
    </span>
  );
}
