// ============================================================================
// Minimal ESLint, existing for one reason: `vite build` does not check whether
// an identifier exists.
//
// Three bugs of exactly that shape shipped in a single afternoon — a component
// referenced without its import, a mock array deleted while its callers stayed,
// and a helper that was never imported at all. Two were invisible until a page
// rendered blank in a browser, because esbuild happily bundles a reference to
// something that is not there and the error only appears at runtime.
//
// So this is deliberately small. `no-undef` is the rule that pays for the whole
// file; everything else here is a rule that would have caught something already
// found by hand.
// ============================================================================
import globals from "globals";

export default [
  {
    files: ["**/*.{js,jsx,mjs}"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: { ...globals.browser, ...globals.node },
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    linterOptions: {
      reportUnusedDisableDirectives: true,
    },
    rules: {
      // The one that matters. A blank page with a 200 and no build error is the
      // worst failure shape available, and this is what prevents it.
      "no-undef": "error",

      // Caught the leftovers from half-finished migrations: a `demotion` flag
      // replaced by a capability comparison, an import kept after its last use.
      // A warning, not an error — an unused variable never broke a page.
      "no-unused-vars": ["warn", {
        args: "after-used",
        argsIgnorePattern: "^_",
        varsIgnorePattern: "^_",
      }],

      // A duplicate import is how one file ends up with two versions of the same
      // module's identifiers and the second silently wins.
      "no-duplicate-imports": "error",
      "no-dupe-keys": "error",
      "no-unreachable": "error",
      // `catch {}` around a real failure is how a broken call looks like a
      // working one. Empty blocks elsewhere are usually a deletion left behind.
      "no-empty": ["error", { allowEmptyCatch: true }],
    },
  },
  {
    // Build scripts run under Node and legitimately use its globals.
    files: ["scripts/**/*.mjs", "*.config.js"],
    languageOptions: { globals: globals.node },
  },
];
