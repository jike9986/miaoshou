"""全量回归：一次跑完所有探针，汇总 PASS/FAIL。

为什么要有这个：探针散在 _meas/ 下 35 个文件里，改一处代码要手动跑十几个
才知道有没有弄坏别处。这个脚本按功能分组跑，一次给出总账。

写新探针前先读 `_meas/README.md` 的三条铁律。最要紧的一条：**必须把
`AOHUANG_DATA_DIR` 指到临时目录**，否则会写真实 app.db（那是用户的 355 件商品）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认 GBK，探针输出里的中文/替换字符会直接抛 UnicodeEncodeError。
# 这里强制 UTF-8，失败时也不炸（errors="replace" 只是兜底，不让汇总脚本自己挂掉）。
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent

# 分组：每组失败都要单独看，因为失败含义不同
GROUPS: dict[str, list[str]] = {
    "核心契约": [
        "probe_bootstrap.py", "probe_confirm_save.py", "probe_release.py",
        "probe_wire3.py", "probe_ro.py", "probe_crypto.py",
    ],
    "定价与模板": [
        "probe_tpl_available.py", "probe_price_sync.py", "probe_fx.py",
        "probe_rate.py", "probe_tr.py",
    ],
    "入仓与闸门": [
        "probe_gate.py", "probe_gate_route.py", "probe_category.py",
        "probe_color.py", "test_pre_save.py", "probe_custom_cat.py",
    ],
    "图片链路": [
        "probe_img_apply.py", "probe_img_wire.py", "probe_public_url.py",
        "probe_images_patch.py", "probe_lang_verify.py",
    ],
    "批次与仓库": [
        "probe_batch.py", "probe_readback.py", "probe_pool.py",
        "probe_prune.py", "probe_log.py",
    ],
    "参考数据": [
        "probe_reference.py", "probe_brand_page.py", "probe_size_chart.py",
        "probe_building.py", "probe_key.py",
    ],
    # 多人共用：这几条测的是「两个人同时用会不会出事」，与功能对不对无关，
    # 所以单独成组——它们红了不代表功能坏了，代表协作保护漏了。
    # probe_concurrency 是唯一真开线程去撞的（其余几条只扫源码/打单线程 HTTP），
    # 它红了就说明保护真的失效，不是「文档没写」那种红。
    "多人协作": [
        "probe_operator.py", "probe_multiuser.py", "probe_multiuser_e2e.py",
        "probe_concurrency.py",
    ],
}


def run(path: Path) -> tuple[int, str, str]:
    """跑一个探针，返回 (returncode, 汇总行, 失败明细)。"""
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=900, cwd=str(HERE.parent))
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT", "超过 900 秒未结束"
    out = (proc.stdout or "") + (proc.stderr or "")
    summary = ""
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("TOTAL"):
            summary = stripped
    fails = [line.strip() for line in out.splitlines()
             if line.strip().startswith("FAIL")]
    if not summary and proc.returncode != 0:
        # 崩溃（不是断言失败）时把最后几行带出来，否则只能看到「exit 1」
        tail = [line for line in out.splitlines() if line.strip()][-4:]
        return proc.returncode, "CRASH", " | ".join(tail)
    return proc.returncode, summary or "NO_SUMMARY", " | ".join(fails[:6])


def main() -> int:
    results: list[dict] = []
    for group, probes in GROUPS.items():
        print(f"\n--- {group} ---")
        for name in probes:
            path = HERE / name
            if not path.exists():
                print(f"SKIP {name}  [文件不存在]")
                results.append({"group": group, "probe": name, "status": "skip",
                                "summary": "文件不存在", "fails": ""})
                continue
            code, summary, fails = run(path)
            ok = code == 0
            print(f"{'PASS' if ok else 'FAIL'} {name}  [{summary}]"
                  + (f"\n      失败: {fails}" if fails else ""))
            results.append({"group": group, "probe": name,
                            "status": "pass" if ok else "fail",
                            "summary": summary, "fails": fails})

    total = sum(1 for r in results if r["status"] != "skip")
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    skipped = sum(1 for r in results if r["status"] == "skip")

    # 把每条探针内部断言的总数也加出来，让「覆盖多少条」有具体数字
    assertions = 0
    for r in results:
        summary = r.get("summary") or ""
        for token in summary.split():
            if token.isdigit() and assertions == 0:
                pass
        parts = summary.split()
        if "TOTAL" in parts:
            try:
                assertions += int(parts[parts.index("TOTAL") + 1])
            except (IndexError, ValueError):
                pass

    print(f"\n{'=' * 60}")
    print(f"探针 {total} 个：{passed} PASS / {failed} FAIL / {skipped} SKIP")
    print(f"内部断言合计：{assertions} 条")
    if failed:
        print("\n失败清单：")
        for r in results:
            if r["status"] == "fail":
                print(f"  - {r['probe']}  [{r['summary']}]")
                if r["fails"]:
                    print(f"      {r['fails']}")

    out = {"probes": total, "pass": passed, "fail": failed, "skip": skipped,
           "assertions": assertions, "details": results}
    (HERE / "probe_all.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细: {HERE / 'probe_all.json'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
