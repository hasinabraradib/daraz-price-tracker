import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        // A restrained, warm-neutral palette instead of Tailwind's
        // default slate/gray — one accent color (a deep teal-green,
        // chosen for "tracking savings" without being the obvious
        // blue-500) does all the "this is interactive/important" work.
        paper: "#FAF8F3",
        surface: "#FFFFFF",
        border: {
          DEFAULT: "#E7E2D6",
          strong: "#D6D0C0",
        },
        ink: {
          DEFAULT: "#221F1A",
          muted: "#6B6558",
          faint: "#9B9587",
        },
        accent: {
          DEFAULT: "#1F6F5C",
          hover: "#17594A",
          soft: "#E5F0EC",
        },
        danger: {
          DEFAULT: "#B3452C",
          soft: "#F5E6E0",
        },
      },
      fontFamily: {
        display: ["var(--font-display)", "serif"],
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "monospace"],
      },
      borderRadius: {
        card: "0.875rem",
      },
      boxShadow: {
        card: "0 1px 2px 0 rgb(34 31 26 / 0.04)",
        "card-hover": "0 4px 16px -4px rgb(34 31 26 / 0.10)",
      },
      transitionTimingFunction: {
        calm: "cubic-bezier(0.4, 0, 0.2, 1)",
      },
    },
  },
  plugins: [],
};
export default config;
