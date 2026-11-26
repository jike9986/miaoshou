/**
 * 验证 E-P0-1 的修复方案：manifest.version 必须校验后才能拼路径。
 *
 * ## 为什么不能只加「黑名单过滤 ..」
 *
 * 常见的第一反应是 `version.replace("..", "")` 或 `if (".." in version) reject`。
 * 这治不了本：
 *
 *     "99..\\..\\.."  → 先替换 .. 得 "99\\\\"  → 还是脏
 *     "..\\.."         → 替换后变空
 *     "0.1.5/../../"  → 只看 ".." 会漏掉 "/" 分隔符
 *
 * 正确做法是**正向白名单**：version 只允许「数字和点」。
 * 真实版本号（0.1.54）天然满足，恶意输入一律不匹配。
 *
 * 本脚本验证：
 *   1. 正向白名单能拦住审计报告里给出的真实 PoC
 *   2. 同时不误伤任何真实版本号
 *   3. 修补后即使绕过（比如未来代码改动），落盘路径也已锁死在 updates 目录内
 *
 * 用法：node docs/audit/_verify_e_p0_1_fix.mjs（从仓库根跑）
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// --- 修法 1：version 正向白名单 ---
const VERSION_RE = /^\d+(\.\d+){0,3}$/;

function isValidVersion(value) {
  const text = String(value ?? "").trim();
  if (!text || text.length > 32) return false;
  return VERSION_RE.test(text);
}

// --- 修法 2：落盘路径必须仍在 updates 目录内（纵深防御）---
function safeInstallerPath(updatesDir, version) {
  const text = String(version ?? "").trim();
  if (!isValidVersion(text)) {
    throw new Error(`非法版本号，拒绝拼路径: ${JSON.stringify(version)}`);
  }
  // ★ 必须用「规范化后的值」拼路径，不能用原始入参。
  //   第一版直接用 version 拼，`" 0.1.54 "` 校验通过后生成的是
  //   `AohuangShangpin- 0.1.54 -Setup.exe`（文件名带空格）。
  //   校验通过 ≠ 可以拿原值去用 —— 中间必须经过规范化。
  const dir = path.resolve(updatesDir);
  const target = path.resolve(
    dir, `AohuangShangpin-${text}-Setup.exe`);
  // 即便版本号校验被绕过，这一步也拦住
  if (target !== dir && !target.startsWith(dir + path.sep)) {
    throw new Error(`安装包路径逃出 updates 目录: ${target}`);
  }
  return target;
}

const updatesDir = path.join(os.tmpdir(), "audit-e-fix-userdata", "updates");

// 审计报告里给出的真实 PoC，外加我补的几种变体
const attacks = [
  ["报告原始 PoC",
   "99..\\..\\..\\..\\..\\..\\Roaming\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\aohuang"],
  ["正斜杠穿越", "0.1.5/../../../evil"],
  ["纯上跳", "../../../evil"],
  ["带空格", "0.1.5 ../evil"],
  ["Windows 设备名", "CON"],
  ["超长", "1".repeat(100)],
  ["空", ""],
  ["null", null],
];

const legit = ["0.1.54", "0.1.5", "1", "10.20.30.40", " 0.1.54 "];

console.log("=== 1. 恶意 version 必须全部被拒 ===");
let allBlocked = true;
for (const [label, value] of attacks) {
  let blocked = false;
  let detail = "";
  try {
    const target = safeInstallerPath(updatesDir, value);
    detail = `放行 → ${target}`;
  } catch (err) {
    blocked = true;
    detail = String(err.message).split(":")[0];
  }
  if (!blocked) allBlocked = false;
  console.log(`  ${blocked ? "✓" : "✗"} ${label.padEnd(16)} ${detail}`);
}

console.log("\n=== 2. 真实版本号不能被误伤 ===");
let allAllowed = true;
for (const value of legit) {
  try {
    const target = safeInstallerPath(updatesDir, value);
    console.log(`  ✓ ${JSON.stringify(value).padEnd(20)} → ${path.basename(target)}`);
  } catch (err) {
    allAllowed = false;
    console.log(`  ✗ ${JSON.stringify(value).padEnd(20)} 被误伤: ${err.message}`);
  }
}

console.log("\n=== 3. 纵深防御：绕过版本校验时路径仍被拦住 ===");
// 模拟「未来有人把版本校验删了」——直接构造穿越路径
const bypass = path.join(updatesDir, `AohuangShangpin-..\\..\\..\\Startup-x-Setup.exe`);
const resolvedBypass = path.resolve(bypass);
const resolvedDir = path.resolve(updatesDir);
const stillContained =
  resolvedBypass === resolvedDir || resolvedBypass.startsWith(resolvedDir + path.sep);
console.log(`  构造穿越路径: ${resolvedBypass}`);
console.log(`  是否仍在 updates 内: ${stillContained}`);
if (!stillContained) {
  console.log("  → 第二层（路径断言）会拦住它 —— 这就是为什么需要两层");
}

console.log("\n=== 4. 服务端也要跟上（is_newer 侧）===");
console.log("  实测 parse_version(\"99..\\\\..\\\\..\") = (99,) > (0,1,54) → is_newer=True");
console.log("  所以服务端「有没有新版本」的判断也会放行恶意版本号。");
console.log("  同一条白名单必须也用在 update_check 的清单校验上。");

console.log("\n=== 结论 ===");
if (allBlocked && allAllowed) {
  console.log("  ✓ 修法成立：正向白名单拦住全部 8 种攻击，且 5 个真实版本号全部放行");
  console.log("");
  console.log("  改动：");
  console.log("    1. updateInstaller.ts 加 VERSION_RE 校验（正向白名单，不是黑名单）");
  console.log("    2. safeInstallerPath() 加路径断言（纵深防御，防未来回退）");
  console.log("    3. server/services/update_check.py 的清单校验复用同一条白名单");
  console.log("    4. server/services/system_service.parse_version 对非法输入返回 () 而非部分解析");
} else {
  console.log("  ✗ 修法有漏洞");
  process.exitCode = 1;
}
