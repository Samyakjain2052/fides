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
//
// THE JSX TRAP, WHICH THIS FILE FELL INTO ONCE
//
// Base ESLint parses JSX but its scope analysis does not treat a JSX element as a
// reference to the identifier that names it. Without eslint-plugin-react that has
// two consequences, and both bit:
//
//   * `no-unused-vars` reports every component that is only ever used in JSX —
//     128 false positives here. Acting on them deletes imports the file needs,
//     which is the same blank-page bug this config exists to prevent.
//   * `no-undef` does NOT catch `<Foo />` with no import for Foo. That was the
//     first of the three bugs, so the config would have missed the one it was
//     most obviously written for.
//
// `react/jsx-uses-vars` fixes the first and `react/jsx-no-undef` the second. They
// are the reason this depends on the plugin at all.
// ============================================================================
import globals from "globals";
import react from "eslint-plugin-react";

export default [
  {
    files: ["**/*.{js,jsx,mjs}"],
    plugins: { react },
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

      // The JSX half of the same rule. `no-undef` cannot see `<Foo />`, so this
      // is what catches a component used without its import — the exact bug that
      // shipped in App.jsx and passed the build.
      "react/jsx-no-undef": "error",

      // Counts a JSX reference as a use. Without it every component import in
      // this codebase reads as dead, and following that advice breaks pages.
      "react/jsx-uses-vars": "error",
      "react/jsx-uses-react": "error",

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
