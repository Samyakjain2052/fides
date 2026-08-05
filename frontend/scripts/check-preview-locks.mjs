#!/usr/bin/env node
// ============================================================================
// Fails the build if a preview module has a mutating control that is not locked.
//
// Phase 0 disabled every such control by hand. Hand-applied guarantees decay:
// the next person adds a Save button to a preview screen and nothing complains,
// and the demo is quietly back to implying a capability it does not have.
//
//   node scripts/check-preview-locks.mjs
// ============================================================================
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("..", import.meta.url).pathname;

// Mutating API functions -> the module whose status governs them.
const MUTATIONS = {
  // updateConsent is gone from the pages: /user/preferences now calls the real
  // API in src/api/consent.js. The three below still write to the mock, and
  // belong to the still-preview public surfaces.
  saveConsentChoices: "consent_surfaces",
  saveCookiePreferences: "consent_surfaces",
  submitGuardianConsent: "consent_surfaces",
  submitGrievance: "grievance",
  submitGrievanceFeedback: "grievance",
  updateGrievance: "grievance",
  updateRetentionPolicy: "retention",
  runPurge: "retention",
  generateReport: "reports",
  saveBreach: "breach",
  sendTestAlert: "notifications",
  updateUser: "users",
  updateDSAR: "dsar_workflow",
  prepareDataExport: "dsar_workflow",
  // submitDSAR is deliberately absent: the dsar module is live.
};

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    return statSync(full).isDirectory() ? walk(full) : full.endsWith(".jsx") ? [full] : [];
  });
}

const modulesSrc = readFileSync(join(ROOT, "src/config/modules.js"), "utf8");
const liveModules = new Set(
  [...modulesSrc.matchAll(/^\s*(\w+):\s*"live"/gm)].map((m) => m[1])
);

const problems = [];
for (const file of walk(join(ROOT, "src/pages"))) {
  const src = readFileSync(file, "utf8");
  const rel = file.replace(ROOT, "");
  const locked = src.includes("previewLock(");

  // Which preview modules does this page mutate?
  const previewMutated = new Set();
  for (const [fn, module] of Object.entries(MUTATIONS)) {
    if (!new RegExp(`\\b${fn}\\(`).test(src)) continue;
    if (liveModules.has(module)) continue;
    previewMutated.add(module);
    if (!locked) {
      problems.push(
        `${rel}: calls ${fn}() for preview module "${module}" but has no ` +
          `previewLock() — the control is not disabled.`
      );
    }
  }

  if (!previewMutated.size) continue;

  // A page-level "has a lock somewhere" test is too coarse. It passed a screen
  // whose primary Save was locked while two shortcut buttons next to it —
  // "Escalate to DPO", "Mark Resolved" — opened a confirm modal and mutated
  // anyway. Every confirm trigger on a mutating page must carry its own lock.
  src.split("\n").forEach((line, i) => {
    if (!/setConfirm\w*\(\s*true\s*\)/.test(line)) return;
    if (line.includes("previewLock(")) return;
    problems.push(
      `${rel}:${i + 1}: opens a confirmation for preview module ` +
        `"${[...previewMutated][0]}" without previewLock() on the same control.`
    );
  });
}

// A fabricated figure is the other half of the same promise.
const apiSrc = readFileSync(join(ROOT, "src/api/index.js"), "utf8");
for (const banned of ["CONSENTS_6M", "DSAR_BY_TYPE_30D"]) {
  if (new RegExp(`^export const ${banned}\\s*=`, "m").test(apiSrc)) {
    problems.push(
      `src/api/index.js: ${banned} is back. It was a hardcoded trend presented ` +
        `as a measurement — derive it from real rows or render an empty state.`
    );
  }
}

if (problems.length) {
  console.error("\npreview-lock check FAILED\n");
  for (const p of problems) console.error("  ✗ " + p);
  console.error("");
  process.exit(1);
}
console.log(`preview-lock check passed (${liveModules.size} live module(s): ${[...liveModules].join(", ")})`);
