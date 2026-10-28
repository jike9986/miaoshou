// web/tailwind.config.js —— 整体替换
/** @type {import('tailwindcss').Config} */

const rgb = (v) => `rgb(var(${v}) / <alpha-value>)`;

export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: { DEFAULT: rgb("--canvas"), inset: rgb("--canvas-inset") },
        sidebar: rgb("--sidebar"),
        surface: {
          DEFAULT: rgb("--surface"),
          hover: rgb("--surface-hover"),
          active: rgb("--surface-active"),
        },
        elevated: rgb("--elevated"),
        inverse: rgb("--inverse-surface"),
        /* 遮罩：变量自带 alpha，直接透传 */
        overlay: "var(--overlay)",
        border: {
          DEFAULT: rgb("--border"),
          subtle: rgb("--border-subtle"),
          strong: rgb("--border-strong"),
        },
        /* 文字：text-fg / text-fg-secondary / text-fg-muted ... */
        fg: {
          DEFAULT: rgb("--text-primary"),
          secondary: rgb("--text-secondary"),
          muted: rgb("--text-muted"),
          disabled: rgb("--text-disabled"),
          inverse: rgb("--text-inverse"),
        },
        brand: {
          50: rgb("--brand-50"),
          100: rgb("--brand-100"),
          200: rgb("--brand-200"),
          300: rgb("--brand-300"),
          400: rgb("--brand-400"),
          500: rgb("--brand-500"),
          600: rgb("--brand-600"),
          700: rgb("--brand-700"),
          800: rgb("--brand-800"),
          900: rgb("--brand-900"),
          950: rgb("--brand-950"),
        },
        success: {
          50: rgb("--success-50"),
          100: rgb("--success-100"),
          500: rgb("--success-500"),
          600: rgb("--success-600"),
          700: rgb("--success-700"),
        },
        warning: {
          50: rgb("--warning-50"),
          100: rgb("--warning-100"),
          500: rgb("--warning-500"),
          600: rgb("--warning-600"),
          700: rgb("--warning-700"),
        },
        danger: {
          50: rgb("--danger-50"),
          100: rgb("--danger-100"),
          500: rgb("--danger-500"),
          600: rgb("--danger-600"),
          700: rgb("--danger-700"),
        },
        info: {
          50: rgb("--info-50"),
          100: rgb("--info-100"),
          500: rgb("--info-500"),
          600: rgb("--info-600"),
          700: rgb("--info-700"),
        },
        chart: {
          1: rgb("--chart-1"),
          2: rgb("--chart-2"),
          3: rgb("--chart-3"),
          4: rgb("--chart-4"),
          5: rgb("--chart-5"),
          6: rgb("--chart-6"),
          7: rgb("--chart-7"),
          8: rgb("--chart-8"),
        },
      },
      fontFamily: {
        sans: [
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Microsoft YaHei",
          "PingFang SC",
          "Hiragino Sans GB",
          "Source Han Sans SC",
          "Noto Sans CJK SC",
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "JetBrains Mono",
          "Cascadia Mono",
          "Consolas",
          "monospace",
        ],
      },
      fontSize: {
        display: ["32px", { lineHeight: "40px", fontWeight: "700", letterSpacing: "-0.02em" }],
        h1: ["24px", { lineHeight: "32px", fontWeight: "600", letterSpacing: "-0.01em" }],
        title: ["18px", { lineHeight: "26px", fontWeight: "600" }],
        h2: ["20px", { lineHeight: "28px", fontWeight: "600", letterSpacing: "-0.005em" }],
        h3: ["16px", { lineHeight: "24px", fontWeight: "600" }],
        /* 覆盖 Tailwind 默认：sm 13/20，base 14/22 */
        base: ["14px", { lineHeight: "22px" }],
        sm: ["13px", { lineHeight: "20px" }],
        xs: ["12px", { lineHeight: "18px" }],
        "2xs": ["11px", { lineHeight: "16px", letterSpacing: "0.02em" }],
        /* 数字专用：与 .num 搭配 */
        "num-lg": ["28px", { lineHeight: "36px", fontWeight: "600", letterSpacing: "-0.01em" }],
      },
      /* 下列间距与 Tailwind 默认刻度完全一致，写出来只为锁定契约；
         若将来 Tailwind 改默认值，这里会把它钉回 4px 基准 */
      spacing: {
        px: "1px",
        0.5: "2px",
        1: "4px",
        1.5: "6px",
        2: "8px",
        2.5: "10px",
        3: "12px",
        3.5: "14px",
        4: "16px",
        5: "20px",
        6: "24px",
        7: "28px",
        8: "32px",
        10: "40px",
        12: "48px",
        16: "64px",
        20: "80px",
      },
      borderRadius: {
        xs: "4px",
        sm: "6px",
        md: "8px",
        lg: "12px",
        xl: "16px",
        "2xl": "20px",
      },
      /* 本文用到的透明度修饰符（/32、/45、/22…）不在 Tailwind 默认刻度里，
         必须在此扩展，否则 bg-brand-600/32、bg-white/92 会被丢弃 */
      opacity: {
        2: "0.02",
        4: "0.04",
        6: "0.06",
        8: "0.08",
        12: "0.12",
        15: "0.15",
        16: "0.16",
        18: "0.18",
        22: "0.22",
        24: "0.24",
        26: "0.26",
        32: "0.32",
        36: "0.36",
        45: "0.45",
        55: "0.55",
        65: "0.65",
        85: "0.85",
        92: "0.92",
      },
      boxShadow: {
        1: "var(--shadow-1)",
        2: "var(--shadow-2)",
        3: "var(--shadow-3)",
        focus: "var(--shadow-focus)",
        /* §5.2 输入框内高光（token 见 styles/tokens-additions.css） */
        inset: "var(--shadow-inset)",
      },
      screens: {
        xs: "375px",
        xs2: "414px",
        sm: "640px",
        md: "768px",
        lg: "1024px",
        xl: "1280px",
        "2xl": "1536px",
        "3xl": "1680px",
      },
      transitionTimingFunction: {
        out: "cubic-bezier(0.16, 1, 0.3, 1)",
        in: "cubic-bezier(0.4, 0, 1, 1)",
        "in-out": "cubic-bezier(0.4, 0, 0.2, 1)",
        spring: "cubic-bezier(0.34, 1.56, 0.64, 1)",
      },
      transitionDuration: {
        instant: "90ms",
        fast: "120ms",
        exit: "140ms",
        card: "160ms",
        base: "180ms",
        image: "200ms",
        medium: "240ms",
        slow: "320ms",
      },
      keyframes: {
        shimmer: {
          "0%": { backgroundPosition: "100% 50%" },
          "100%": { backgroundPosition: "0% 50%" },
        },
        "status-breathe": {
          "0%, 100%": { opacity: "1", transform: "scale(1)" },
          "50%": { opacity: "0.45", transform: "scale(0.86)" },
        },
        spin: {
          to: { transform: "rotate(360deg)" },
        },
      },
      animation: {
        shimmer: "shimmer 1.6s linear infinite",
        "status-breathe": "status-breathe 1.8s ease-in-out infinite",
        spin: "spin 0.6s linear infinite",
      },
    },
  },
  // ESM 下不能用 require()。若需要 tailwindcss-animate，先
  // pnpm add -D tailwindcss-animate，再改为：
  //   import animate from "tailwindcss-animate";
  //   plugins: [animate],
  plugins: [],
};
