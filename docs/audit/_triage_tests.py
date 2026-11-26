"""把 96 个测试失败逐个归类：测试代码过时 vs 生产代码真错。

这是审计最关键的判断 —— 直接决定「能不能修」以及「修哪边」。

分类标准：
  TEST_STALE  : 生产签名/业务口径正确，测试没跟上 → 改测试
  PROD_BUG    : 测试是对的，生产行为错误 → 改生产代码
  ENV         : 环境依赖（外部服务、fixture 缺失）→ 补 fixture 或标记
  UNKNOWN     : 需要人工判断

用法：cd miaoshou/server && python ../docs/audit/_triage_tests.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2] / "server"


def run_pytest() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--tb=line", "-q",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(SERVER),
    )
    return result.stdout + result.stderr


# 已知的判定规则：(正则, 分类, 说明)
RULES: list[tuple[str, str, str]] = [
    # --- 测试桩签名没跟上生产 ---
    (r"fill_carriers\(\) got an unexpected keyword argument 'category_trees'",
     "TEST_STALE",
     "生产 fill_carriers 已加 category_trees 参数（154a21d），测试桩没加"),
    (r"<lambda>\(\) got an unexpected keyword argument 'operator_id'",
     "TEST_STALE",
     "生产函数已加 operator_id 参数（多租户隔离），测试 lambda 桩没加"),
    (r"fixture 'probe_runner' not found",
     "ENV",
     "test_parity 依赖的 probe_runner fixture 从未存在于 conftest"),

    # --- 业务口径变更，测试停留在旧值 ---
    (r"assert Decimal\('0\.06'\) == Decimal\('0\.18'\)",
     "TEST_STALE",
     "马来运费 0.18→0.06 是妙手官方运价（70086d6），测试仍是旧值"),
    (r"assert Decimal\('0\.22'\) == Decimal\('0\.42'\)",
     "TEST_STALE",
     "续重价 0.12→0.08 是官方运价，测试仍是旧值"),
    (r"assert Decimal\('0\.14'\) == Decimal\('0\.30'\)",
     "TEST_STALE",
     "续重取整口径随官方运价变化，测试仍是旧值"),

    # --- 生产行为被静默改变，测试是对的 ---
    (r"assert verify_readback is True|发布路径必须开启读回核验",
     "PROD_BUG",
     "ff5178f(11-07) 建立「发布强制读回核验」契约，fd826fa(11-19) 改成 False 且 commit message 未提"),

    # --- 错误文案/诊断口径变更 ---
    (r"仓库是空的，先去采集箱处理商品",
     "UNKNOWN",
     "诊断文案变了：测试期望「仓库里没有属于这个账号的商品」，实际返回「仓库是空的」"),
]


def classify(line: str) -> tuple[str, str] | None:
    for pattern, kind, note in RULES:
        if re.search(pattern, line):
            return kind, note
    return None


def main() -> None:
    print("正在跑全量测试…", file=sys.stderr)
    out = run_pytest()
    raw_path = Path(tempfile.gettempdir()) / "audit_triage_raw.txt"
    raw_path.write_text(out, encoding="utf-8")

    failed = re.findall(r"^FAILED (tests/[^\s]+)", out, re.M)
    errored = re.findall(r"^ERROR (tests/[^\s]+)", out, re.M)
    print(f"失败 {len(failed)} 个，错误 {len(errored)} 个，合计 {len(failed) + len(errored)}")

    # 抓每个失败对应的 E 行
    e_lines = [l for l in out.splitlines() if l.strip().startswith("E ")]
    e_blob = "\n".join(e_lines)

    buckets: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for pattern, kind, note in RULES:
        hits = re.findall(pattern, e_blob)
        if hits:
            buckets[kind].append((f"{len(hits)} 次命中", note))

    print("\n=== 按规则归类的证据 ===")
    for kind in ("TEST_STALE", "PROD_BUG", "ENV", "UNKNOWN"):
        if kind in buckets:
            print(f"\n[{kind}]")
            for count, note in buckets[kind]:
                print(f"  {count}: {note}")

    # 按文件统计
    print("\n=== 失败按文件分布 ===")
    per_file: dict[str, int] = defaultdict(int)
    for f in failed + errored:
        per_file[f.split("::")[0]] += 1
    for f, n in sorted(per_file.items(), key=lambda kv: -kv[1]):
        print(f"  {n:3d}  {f}")

    print("\n原始输出已存 server/_triage_raw.txt")


if __name__ == "__main__":
    main()
