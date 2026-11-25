"""用真实 CSS 量出模板按钮的渲染宽度，不再靠公式推算。

做法：把安装包里那份 CSS 和一段等价的 DOM 拼成页面，用 Playwright 按
不同窗口宽度渲染，直接读 getBoundingClientRect + computedStyle。
"""
import json
import pathlib
import sys

DIST = pathlib.Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\web\dist\assets")
css_file = next(DIST.glob("index-*.css"))
css = css_file.read_text(encoding="utf-8")
print(f"用 {css_file.name}，{len(css)/1024:.0f} KB")

# 与 SchedulesPage.tsx 里 1235 行 / 1331 行完全一致的 class
TEMPLATE_ROW = "grid gap-3 md:grid-cols-[148px_minmax(0,1fr)]"
TPL_BOX = "grid grid-cols-[repeat(auto-fit,minmax(60px,1fr))] gap-1.5"
BTN = ("flex w-full items-center justify-center gap-1 rounded-md border "
       "px-1 py-1.5 text-xs transition-colors duration-fast")
BTN_ACTIVE = "border-brand-600 bg-brand-50 font-medium text-brand-700"
BTN_IDLE = "border-border text-fg-secondary"

names = ["马来", "泰国", "越南", "菲律宾", "新加坡"]

def buttons():
    out = []
    for i, n in enumerate(names):
        cls = BTN + " " + (BTN_ACTIVE if i == 0 else BTN_IDLE)
        check = ('<svg class="size-2.5 shrink-0" viewBox="0 0 12 12" '
                 'fill="none" stroke="currentColor" stroke-width="2">'
                 '<path d="M1 6l3 3 7-7"/></svg>') if i == 0 else ""
        out.append(
            f'<button class="{cls}" data-tpl="{n}">{check}'
            f'<span class="min-w-0 truncate">{n}</span></button>'
        )
    return "".join(out)

html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>{css}</style></head><body>
<!-- 模拟 Dialog lg: max-w-[640px]，内容 px-6 -->
<div style="width:100%;max-width:640px;padding:0 24px;box-sizing:border-box">
  <div id="row" class="{TEMPLATE_ROW}">
    <div id="shopcol" class="flex flex-col gap-1.5">
      <div><label class="text-sm font-medium leading-[18px]">目标店铺<span class="text-danger-600">*</span></label></div>
      <div style="height:36px;border:1px solid #ccc;border-radius:6px"></div>
    </div>
    <div id="tplcol" class="flex flex-col gap-1.5">
      <div><label class="text-sm font-medium leading-[18px]">定价模板</label></div>
      <div id="tplbox" class="{TPL_BOX}">{buttons()}</div>
    </div>
  </div>
</div>
</body></html>"""

path = pathlib.Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\tools\source_match\_layout_probe.html")
path.write_text(html, encoding="utf-8")

from playwright.sync_api import sync_playwright

report = []
with sync_playwright() as pw:
    b = pw.chromium.launch()
    page = b.new_page()
    page.goto(path.as_uri())
    for width in (673, 768, 900, 1100, 1280):
        page.set_viewport_size({"width": width, "height": 800})
        page.wait_for_timeout(120)
        data = page.evaluate("""() => {
          const r = document.getElementById('row');
          const shop = document.getElementById('shopcol');
          const tpl = document.getElementById('tplcol');
          const box = document.getElementById('tplbox');
          const btns = [...document.querySelectorAll('[data-tpl]')];
          const cs = getComputedStyle(box);
          const rect = el => { const b = el.getBoundingClientRect();
            return {w: +b.width.toFixed(1), h: +b.height.toFixed(1),
                    top: +b.top.toFixed(1), left: +b.left.toFixed(1)}; };
          // 每行几个：按 top 分组
          const tops = [...new Set(btns.map(b => +b.getBoundingClientRect().top.toFixed(1)))];
          return {
            rowW: +r.getBoundingClientRect().width.toFixed(1),
            rowCols: getComputedStyle(r).gridTemplateColumns,
            shop: rect(shop), tpl: rect(tpl), box: rect(box),
            boxW: box.getBoundingClientRect().width.toFixed(1),
            btnsPerRow: btns.length / tops.length,
            rows: tops.length,
            btnW: btns.map(b => +b.getBoundingClientRect().width.toFixed(1)),
            btnTextOverflow: btns.map(b => {
              const s = b.querySelector('span');
              return s.scrollWidth > s.clientWidth + 0.5 ? 'TRUNCATED' : 'ok';
            }),
            boxCols: cs.gridTemplateColumns,
          };
        }""")
        report.append((width, data))
    b.close()

print()
for width, d in report:
    print(f"=== 窗口 {width}px ===")
    print(f"  行宽 {d['rowW']}  列: {d['rowCols']}")
    print(f"  店铺 {d['shop']['w']}  模板 {d['tpl']['w']}")
    print(f"  模板容器 {d['boxW']}  列: {d['boxCols']}")
    print(f"  按钮宽度 {d['btnW']}")
    print(f"  → {d['btnsPerRow']:.0f} 个/行，共 {d['rows']} 行")
    print(f"  文字溢出: {d['btnTextOverflow']}")
    print()

out = pathlib.Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\tools\source_match\_layout_report.json")
out.write_text(json.dumps([{"width": w, **d} for w, d in report],
                          ensure_ascii=False, indent=2), encoding="utf-8")
print(f"明细写入 {out.name}")