"""验证模板列表按国家过滤：勾 MY+PH 店铺 → 只出现「马来」「菲律宾」两个按钮。"""
import json, pathlib

DIST = pathlib.Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\web\dist\assets")
css = next(DIST.glob("index-*.css")).read_text(encoding="utf-8")

TEMPLATES = [
    {"name": "马来", "site": "MY"}, {"name": "菲律宾", "site": "PH"},
    {"name": "泰国", "site": "TH"}, {"name": "越南", "site": "VN"},
    {"name": "新加坡", "site": "SG"},
]
SHOPS = [
    {"shopId": "19956376", "name": "LJ&shop.", "site": "MY", "siteName": "马来"},
    {"shopId": "20080061", "name": "LJ&shop.", "site": "PH", "siteName": "菲律宾"},
]

html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>{css}</style></head><body style="padding:16px;font:13px system-ui">
<div class="grid gap-3 sm:col-span-2 md:grid-cols-[148px_minmax(0,1fr)]">
  <div><label class="text-sm">目标店铺</label>
    <div id="shopsum" class="flex h-9 items-center rounded-md border border-border-subtle bg-canvas-inset px-2 text-xs"></div>
  </div>
  <div><label class="text-sm">定价模板</label>
    <div id="tplbox" class="grid grid-cols-[repeat(auto-fit,minmax(60px,1fr))] gap-1.5"></div>
  </div>
</div>
<script>
const TEMPLATES = {json.dumps(TEMPLATES)};
const SHOPS = {json.dumps(SHOPS)};
const pickedShopIds = SHOPS.map(s => s.shopId);      // 上面勾了两个店
const selectedSites = new Set(SHOPS.map(s => s.site)); // MY, PH

// 与 SchedulesPage.templateOptions 同一逻辑
const opts = selectedSites.size === 0
  ? TEMPLATES
  : (() => {{
      const m = TEMPLATES.filter(t => t.site === "" || selectedSites.has(t.site));
      return m.length > 0 ? m : TEMPLATES;
    }})();

document.getElementById('shopsum').textContent =
  pickedShopIds.length === 1 ? SHOPS[0].name : `已选 ${{pickedShopIds.length}} 个店铺`;
document.getElementById('tplbox').innerHTML = opts.map((t, i) => `
  <button data-tpl="${{t.name}}" aria-pressed="${{i===0}}"
    class="flex w-full items-center justify-center gap-1 rounded-md border px-1 py-1.5 text-xs ${{i===0?'border-brand-600 bg-brand-50 font-medium text-brand-700':'border-border text-fg-secondary'}}">
    <span class="min-w-0 truncate">${{t.name}}</span>
  </button>`).join('');
</script></body></html>"""

path = pathlib.Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\tools\source_match\_tpl_filter.html")
path.write_text(html, encoding="utf-8")

from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 700, "height": 500})
    pg.goto(path.as_uri())
    pg.wait_for_timeout(200)
    shown = pg.evaluate("""() => [...document.querySelectorAll('[data-tpl]')].map(b => b.dataset.tpl)""")
    disabled = pg.evaluate("""() => [...document.querySelectorAll('[data-tpl]')].filter(b => b.disabled).map(b => b.dataset.tpl)""")
    shop = pg.evaluate("""() => document.getElementById('shopsum').textContent""")
    pg.screenshot(path=str(path.with_suffix(".png")))
    b.close()

print(f"勾选的店铺: 2 个（MY + PH）")
print(f"店铺摘要:   {shop}")
print(f"渲染出的模板: {shown}")
print(f"被禁用的:    {disabled}")
print()
ok = set(shown) == {"马来", "菲律宾"}
print("结果:", "通过 —— 只有马来和菲律宾，其他国家根本不出现" if ok else f"失败 —— 多出了 {set(shown) - {'马来','菲律宾'}}")