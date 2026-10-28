// web/eslint.config.js —— ESLint 9 flat config（ESLint 9 已废弃 .eslintrc 旧格式）
//
// 取舍原则：19k+ 行存量代码，只开「能抓真 bug」和「不影响协作流畅」的规则。
// 明确不开 recommended-type-checked：需要为整个项目生成类型信息，跑一次几十秒，
// 且会在存量代码上报出几百条噪音，最终结果是没人跑 lint。
// 想升级到 type-checked 时，建议先对新增文件用 overrides 灰度，不要一把梭。
//
// 与 Prettier 的分工：格式问题一律交给 Prettier，这里只留 eslint-config-prettier
// 关掉的冲突规则，不做二次格式化。

import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import prettierConfig from "eslint-config-prettier";
import globals from "globals";

export default tseslint.config(
  {
    // 不参与 lint 的目录/文件。
    // dist：构建产物；node_modules：依赖；*.log / tsbuildinfo：工具生成物。
    ignores: ["dist/**", "node_modules/**", "*.log", "tsconfig.tsbuildinfo"],
  },

  // JS/TS 基础推荐集（非 type-checked）
  js.configs.recommended,
  ...tseslint.configs.recommended,

  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: {
        ...globals.browser,
        ...globals.es2022,
      },
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      // ── react-hooks ────────────────────────────────────────────────────────
      // rules-of-hooks 必须是 error：在条件/循环里调 hook 会直接让 React 运行时炸，
      // 这是「提交前必须拦住」的那类错误。
      "react-hooks/rules-of-hooks": "error",
      // exhaustive-deps 先 warn：存量代码里几乎肯定有一批「故意不写全依赖」的场景
      // （稳定引用、只跑一次的初始化），一上来设 error 会逼人写 eslint-disable 而不是修。
      "react-hooks/exhaustive-deps": "warn",

      // ── react-refresh ─────────────────────────────────────────────────────
      // 一个文件同时导出组件和非组件会让 HMR 退化到整页刷新，只影响开发体验，故 warn。
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],

      // ── 自定义规则 ─────────────────────────────────────────────────────────
      // console.log 留在生产代码里通常是调试残留；warn/error 是业务需要的日志出口。
      "no-console": ["warn", { allow: ["warn", "error"] }],

      // 未使用变量 = error。tsconfig 已开 noUnusedLocals/noUnusedParameters，
      // 这条与之一致，并且能覆盖 tsc 看不到的情况（如仅类型导入）。
      // argsIgnorePattern "^_" 与 TS 的约定一致：下划线开头 = 有意不使用。
      "@typescript-eslint/no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
          destructuredArrayIgnorePattern: "^_",
        },
      ],

      // 禁深层相对导入，统一走 @/ 别名（vite.config.ts 与 tsconfig paths 已配好）。
      // 存量实测 0 处违反，所以这里直接给 warn 作为「新代码别写错」的护栏。
      "no-restricted-imports": [
        "warn",
        {
          patterns: [
            {
              group: ["../../*"],
              message: "禁止 ../../ 深层相对导入，请用 @/ 别名（如 @/lib/utils）。",
            },
          ],
        },
      ],
    },
  },

  // 构建/配置文件：跑在 Node 里，且需要 console 输出
  {
    files: ["vite.config.ts", "tailwind.config.js", "postcss.config.js", "eslint.config.js"],
    languageOptions: {
      globals: { ...globals.node },
    },
    rules: {
      "no-console": "off",
    },
  },

  // eslint-config-prettier 必须放最后：关掉所有与 Prettier 冲突的格式规则。
  prettierConfig,
);
