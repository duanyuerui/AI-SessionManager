/** Tailwind 配置：仅用于本地构建 vendor/tailwind.css，运行时不需要 node。 */
const path = require("path");

module.exports = {
  content: [path.join(__dirname, "index.html")],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        bg: "rgb(var(--c-bg) / <alpha-value>)",
        surface: "rgb(var(--c-surface) / <alpha-value>)",
        "surface-2": "rgb(var(--c-surface-2) / <alpha-value>)",
        line: "rgb(var(--c-line) / <alpha-value>)",
        "line-strong": "rgb(var(--c-line-strong) / <alpha-value>)",
        ink: "rgb(var(--c-ink) / <alpha-value>)",
        "ink-2": "rgb(var(--c-ink-2) / <alpha-value>)",
        muted: "rgb(var(--c-muted) / <alpha-value>)",
        faint: "rgb(var(--c-faint) / <alpha-value>)",
        accent: {
          DEFAULT: "rgb(var(--c-accent) / <alpha-value>)",
          soft: "rgb(var(--c-accent-soft) / <alpha-value>)",
        },
        danger: {
          DEFAULT: "rgb(var(--c-danger) / <alpha-value>)",
          soft: "rgb(var(--c-danger-soft) / <alpha-value>)",
        },
        success: "rgb(var(--c-success) / <alpha-value>)",
      },
      fontFamily: {
        sans: [
          "-apple-system", "BlinkMacSystemFont", '"Segoe UI"', '"Microsoft YaHei"',
          '"PingFang SC"', '"Hiragino Sans GB"', "sans-serif",
        ],
        mono: ['ui-monospace', '"Cascadia Mono"', "Consolas", '"Courier New"', "monospace"],
      },
      boxShadow: {
        card: "0 1px 2px rgb(0 0 0 / 0.04), 0 1px 3px rgb(0 0 0 / 0.05)",
        float: "0 10px 30px -8px rgb(0 0 0 / 0.25), 0 2px 8px rgb(0 0 0 / 0.08)",
      },
      keyframes: {
        "toast-in": {
          from: { opacity: "0", transform: "translateY(8px) scale(.98)" },
          to: { opacity: "1", transform: "none" },
        },
        "rise": {
          from: { opacity: "0", transform: "translateY(10px)" },
          to: { opacity: "1", transform: "none" },
        },
      },
      animation: {
        "toast-in": "toast-in .16s ease-out",
        rise: "rise .18s ease-out",
      },
    },
  },
  plugins: [],
};
