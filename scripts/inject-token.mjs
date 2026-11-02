#!/usr/bin/env node
/**
 * 把客户端令牌写进 build/client-token.json，随安装包一起分发。
 *
 * 令牌不进 git（.gitignore 已排除），只在打包时从环境变量或本地文件读取：
 *   AOHUANG_REMOTE_CLIENT_TOKEN=xxx node scripts/inject-token.mjs
 * 或者把令牌放在 _client_token.txt（同样被 git 忽略）。
 */
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const rootDir = join(here, "..");              // tk-desktop/
const appDir = join(rootDir, "app");           // tk-desktop/app/
const outFile = join(appDir, "build", "client-token.json");

function fromEnv() {
  return (process.env.AOHUANG_REMOTE_CLIENT_TOKEN || "").trim();
}

function fromFile() {
  const candidates = [
    join(rootDir, "..", "_client_token.txt"),
    join(rootDir, "_client_token.txt"),
    join(appDir, "build", "client-token.txt"),
  ];
  for (const file of candidates) {
    if (!existsSync(file)) continue;
    const value = readFileSync(file, "utf-8").trim();
    if (value) return value;
  }
  return "";
}

const token = fromEnv() || fromFile();
if (!token) {
  console.error("[inject-token] 没有拿到客户端令牌：设置 AOHUANG_REMOTE_CLIENT_TOKEN 或创建 _client_token.txt");
  process.exit(1);
}

writeFileSync(outFile, JSON.stringify({ token }, null, 2) + "\n", "utf-8");
console.log(`[inject-token] 已写入 ${outFile}（长度 ${token.length}）`);
