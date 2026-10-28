// app/eslint.config.js —— ESLint 9 flat config（Electron 主进程，Node 环境）
//
// 为什么是 CommonJS 写法：app/package.json 没有 "type": "module"（Electron 主进程
// 由 tsc 编译成 CommonJS 到 dist/，加 "type": "module" 会直接让主进程跑不起来），
// 所以这个 .js 文件会被 Node 当成 CJS。ESLint 9 支持 CJS 形式的 flat config，
// 用 require + module.exports 即可，不用改成 .mjs 后缀。
//
// 与 web/eslint.config.js 同一套取舍：只开「能抓真 bug」的规则，不引入
// type-checked 推荐集（存量代码会被几百条噪音淹没，最后没人跑）。
// 主进程没有 React，所以不装 react / react-hooks / react-refresh 插件。
//
// 格式一律交给 Prettier，eslint-config-prettier 放最后关掉冲突规则。

const js = require("@eslint/js");
const tseslint = require("typescript-eslint");
const prettierConfig = require("eslint-config-prettier");
const globals = require("globals");

module.exports = tseslint.config(
  {
    // dist 是 tsc 的编译产物，lint 它等于自己跟自己较劲
    ignores: ["dist/**", "node_modules/**", "build/**", "*.log"],
  },

  // JS/TS 基础推荐集（非 type-checked）
  js.configs.recommended,
  ...tseslint.configs.recommended,

  {
    files: ["**/*.{ts,tsx,js,cjs,mjs}"],
    languageOptions: {
      ecmaVersion: 2022,
      // 主进程是 CommonJS（tsconfig module: CommonJS），但源码用 ESM 语法书写，
      // 由 tsc 编译转换，所以这里按 module 解析。
      sourceType: "module",
      globals: {
        ...globals.node,
        ...globals.es2022,
      },
    },
    rules: {
      // ── 自定义规则（与 web 保持一致，便于同一套肌肉记忆） ────────────────
      // 主进程的 console 是唯一的日志出口（写日志文件也走它），但 log 级别的
      // 调试残留同样应该被提醒；warn/error 放行。
      "no-console": ["warn", { allow: ["warn", "error"] }],

      // 未使用变量 = error；下划线开头表示有意不用（与 web 同约定）。
      "@typescript-eslint/no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
          destructuredArrayIgnorePattern: "^_",
        },
      ],

      // 主进程 import 的是 node 内置模块和 electron，没有 @/ 别名，
      // 相对导入天然只有 ./x 一级，所以这里不启用 no-restricted-imports。
    },
  },

  // eslint 配置文件自身：这个文件是 CJS（见文件头说明），必须用 require，
  // 所以对本文件关掉 no-require-imports。
  {
    files: ["eslint.config.js"],
    rules: {
      "@typescript-eslint/no-require-imports": "off",
    },
  },

  // eslint-config-prettier 必须放最后：关掉所有与 Prettier 冲突的格式规则。
  prettierConfig,
);
