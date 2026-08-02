/** @type {import('tailwindcss').Config} */
// The design system from the brief, verbatim. Use these token names everywhere
// (bg-navy, text-muted, border-line …) rather than raw hex, so a palette change
// is one edit.
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        navy: "#1A3C5E",       // Primary
        teal: "#0D7377",       // Accent
        success: "#22C55E",    // Active / Valid
        warning: "#F59E0B",    // Pending / Expiring soon
        danger: "#EF4444",     // Withdrawn / Expired / Rejected
        info: "#2563EB",       // In progress
        canvas: "#F8FAFC",     // Background
        surface: "#FFFFFF",    // Card surface
        line: "#E2E8F0",       // Border
        ink: "#1E293B",        // Text primary
        muted: "#64748B",      // Text muted
      },
      fontFamily: {
        sans: [
          "Inter",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "sans-serif",
        ],
      },
      borderRadius: { xl: "0.75rem" },
      boxShadow: {
        sm: "0 1px 2px 0 rgb(16 24 40 / 0.05)",
        panel: "-4px 0 24px 0 rgb(16 24 40 / 0.10)",
      },
    },
  },
  plugins: [],
};
