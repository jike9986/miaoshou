"""对比两种方案在各窗口宽度下的真实渲染，选不会截断的那个。

方案 A（当前）：grid auto-fit minmax(60px,1fr) + 按钮 w-full + truncate
方案 B（候选）：flex flex-wrap + 按钮自然宽度 + whitespace-nowrap，不截断
"""
import json
import pathlib

DIST = pathlib.Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\web\dist\assets")
css = next(DIST.glob("index-*.css")).read_text(encoding="utf-8")

names = ["马来", "泰国", "越南", "菲律宾", "新加坡"]

BTN_BASE = ("flex items-center justify-center gap-1 rounded-md border "
            "py-1.5 text-xs transition-colors duration-fast")
BTN_ACTIVE = "border-brand-600 bg-brand-50 font-medium text-brand-700"
BTN_IDLE = "border-border text-fg-secondary"
CHECK = ('<svg class="size-3 shrink-0" viewBox="0 0 12 12" fill="none" '
         'stroke="currentColor" stroke-width="2"><path d="M1 6l3 3 7-7"/></svg>')


def btns(mode):
    out = []
    for i, n in enumerate(names):
        cls = f"{BTN_BASE} {BTN_ACTIVE if i == 0 else BTN_IDLE}"
        if mode == "A":
            cls += " w-full px-1"
            inner = f'<span class="min-w-0 truncate">{n}</span>'
        else:
            cls += " px-3 whitespace-nowrap"
            inner = f'<span>{n}</span>'
        check = CHECK if i == 0 else ""
        out.append(f'<button class="{cls}" data-tpl="{n}">{check}{inner}</button>')
    return "".join(out)


def page(mode, container):
    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>{css}</style></head><body>
<div style="width:100%;max-width:640px;padding:0 24px;box-sizing:border-box">
 <div class="grid gap-3 sm:grid-cols-2">
  <div class="sm:col-span-2"><div style="height:40px;border:1px dashed #ddd">发布账号区</div></div>
  <div id="rowwrap" class="sm:col-span-2">
  <div class="grid gap-3 md:grid-cols-[148px_minmax(0,1fr)]">
    <div class="flex flex-col gap-1.5">
      <label class="text-sm font-medium leading-[18px]">目标店铺<span class="text-danger-600">*</span></label>
      <div style="height:36px;border:1px solid #ccc;border-radius:6px"></div>
    </div>
    <div class="flex flex-col gap-1.5">
      <label class="text-sm font-medium leading-[18px]">定价模板</label>
      <div id="tplbox" class="{container}">{btns(mode)}</div>
    </div>
  </div>
  </div>
 </div>
</div>
</body></html>"""


CASES = [
    ("A", "grid grid-cols-[repeat(auto-fit,minmax(60px,1fr))] gap-1.5"),
    ("B", "flex flex-wrap gap-1.5"),
]

from playwright.sync_api import sync_playwright

tmp = pathlib.Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\tools\source_match\_layout_probe.html")
results = []
with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page()
    for mode, container in CASES:
        tmp.write_text(page(mode, container), encoding="utf-8")
        for width in (538, 673, 768, 900, 1100, 1440):
            pg.goto(tmp.as_uri())
            pg.set_viewport_size({"width": width, "height": 800})
            pg.wait_for_timeout(100)
            d = pg.evaluate("""() => {
              const box = document.getElementById('tplbox');
              const btns = [...document.querySelectorAll('[data-tpl]')];
              const tops = [...new Set(btns.map(b => +b.getBoundingClientRect().top.toFixed(1)))];
              return {
                boxW: +box.getBoundingClientRect().width.toFixed(1),
                btnW: btns.map(b => +b.getBoundingClientRect().width.toFixed(1)),
                perRow: btns.length / tops.length,
                rows: tops.length,
                truncated: btns.map(b => {
                  const s = b.querySelector('span');
                  return s.scrollWidth > s.clientWidth + 0.5;
                }),
              };
            }""")
            results.append({"mode": mode, "width": width, **d})
    b.close()

print()
for mode, _ in CASES:
    print(f"===== 方案 {mode} =====")
    for r in results:
        if r["mode"] != mode:
            continue
        bad = "截断!" if any(r["truncated"]) else "完整"
        print(f"  {r['width']:>5}px  容器 {r['boxW']:>6}  "
              f"{r['perRow']:.0f}个/行 x{r['rows']}行  "
              f"按钮宽 {[round(w) for w in r['btnW']]}  文字:{bad}")
    print()

pathlib.Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\tools\source_match\_layout_report.json")\
    .write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")