#!/usr/bin/env node
/**
 * 静态设计护栏：只检查能确定的问题，不假装替代视觉/可访问性验收。
 * - 业务 TSX 不写十六进制颜色
 * - 不使用已经从 Token 体系删除的旧类名/未定义色阶
 * - focus-visible:outline-none 附近必须有替代焦点环
 * - 根 DESIGN.md、Token 和 reduced-motion 护栏必须存在
 */
import { readdirSync, readFileSync, existsSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(webRoot, "..");
const srcRoot = join(webRoot, "src");
const findings = [];

function walk(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? walk(path) : path.endsWith(".tsx") ? [path] : [];
  });
}

function lineOf(text, index) {
  return text.slice(0, index).split("\n").length;
}

function report(file, text, index, message) {
  findings.push(`${relative(repoRoot, file)}:${lineOf(text, index)} ${message}`);
}

const legacyTokens = new Map([
  [/\btext-fg-subtle\b/g, "text-fg-subtle → text-fg-muted"],
  [/\bborder-line\b/g, "border-line → border-border"],
  [/\bbg-surface-sunken\b/g, "bg-surface-sunken → bg-canvas-inset"],
  [/\bbg-surface-2\b/g, "bg-surface-2 → bg-canvas-inset"],
  [/\bbg-overlay\/\d+\b/g, "overlay 变量自带透明度；直接使用 bg-overlay"],
  [/\bbg-black(?:\/|\b)/g, "交互态不能绕过语义表面；使用 surface-hover/surface-active"],
  [/\b(?:border|bg|text)-(?:success|warning|danger|info)-(?:200|300|400|800|900)\b/g, "未定义的状态色阶；使用已声明的 50/100/500/600/700"],
]);

for (const file of walk(srcRoot)) {
  const text = readFileSync(file, "utf8");

  for (const [pattern, message] of legacyTokens) {
    pattern.lastIndex = 0;
    for (const match of text.matchAll(pattern)) {
      report(file, text, match.index, `无效设计 Token：${match[0]}（${message}）`);
    }
  }

  // TSX 的颜色必须来自 tokens.css。忽略注释行，避免规格说明误报。
  const lines = text.split("\n");
  let offset = 0;
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("//") && !trimmed.startsWith("*") && !trimmed.startsWith("/*")) {
      const color = line.match(/#[0-9a-fA-F]{3,8}\b/);
      if (color) report(file, text, offset + color.index, `业务组件硬编码颜色 ${color[0]}；改用语义 Token`);
    }
    offset += line.length + 1;
  }

  for (const match of text.matchAll(/focus-visible:outline-none/g)) {
    const before = text.slice(Math.max(0, match.index - 120), match.index);
    const after = text.slice(match.index, match.index + 280);
    if (!before.includes("focus-visible:ring") && !after.includes("focus-visible:ring")) {
      report(file, text, match.index, "去掉键盘焦点后没有提供 focus-visible:ring 替代样式");
    }
  }
}

for (const required of [
  join(repoRoot, "DESIGN.md"),
  join(repoRoot, "docs", "design-system.md"),
  join(srcRoot, "styles", "tokens.css"),
]) {
  if (!existsSync(required)) findings.push(`${relative(repoRoot, required)} 缺少设计规范入口`);
}

const tokensPath = join(srcRoot, "styles", "tokens.css");
if (existsSync(tokensPath)) {
  const tokens = readFileSync(tokensPath, "utf8");
  if (!tokens.includes("@media (prefers-reduced-motion: reduce)")) {
    findings.push("web/src/styles/tokens.css 缺少 prefers-reduced-motion 护栏");
  }
}

if (findings.length > 0) {
  console.error(`设计规范检查失败（${findings.length} 项）：\n${findings.map((item) => `- ${item}`).join("\n")}`);
  process.exit(1);
}

console.log(`设计规范检查通过：${walk(srcRoot).length} 个 TSX 文件；Token、焦点与 reduced-motion 护栏有效。`);
