"""多账号场景验证：勾「梁静」账号 + 其下 MY/PH 两个店 → 模板只剩马来+菲律宾。

复现的关键点：被勾的账号是**别的**账号（不是当前登录账号），
它的店铺在 shopsByAccount[accId] 里，不在 shopOptions 里。
这正是之前「勾了马来和菲律宾，其他国家模板还能点」的原因。
"""
import json, pathlib

DIST = pathlib.Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\web\dist\assets")
css = next(DIST.glob("index-*.css")).read_text(encoding="utf-8")

TEMPLATES = [
    {"name": "马来", "site": "MY"}, {"name": "菲律宾", "site": "PH"},
    {"name": "泰国", "site": "TH"}, {"name": "越南", "site": "VN"},
    {"name": "新加坡", "site": "SG"},
]
# 账号 9「梁静」名下的两个店（生产库实际数据）
SHOPS_BY_ACCOUNT = {
    "9": [
        {"shopId": "19956376", "name": "LJ&shop.", "site": "MY", "siteName": "马来"},
        {"shopId": "20080061", "name": "LJ&shop.", "site": "PH", "siteName": "菲律宾"},
    ],
    "7": [{"shopId": "19758309", "name": "Linen Haven MY", "site": "MY", "siteName": "马来"}],
}
CHECKED_ACCOUNTS = ["9"]                      # 只勾了梁静
PICKED = {"9": ["19956376", "20080061"]}      # 它下面两个店都勾了

html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>{css}</style></head><body style="padding:16px;font:13px system-ui">
<div class="sm:col-span-2">
  <label class="text-sm font-medium">定价模板</label>
  <div id="tplbox" class="mt-1.5 grid grid-cols-[repeat(auto-fit,minmax(60px,1fr))] gap-1.5"></div>
</div>
<script>
const TEMPLATES = {json.dumps(TEMPLATES, ensure_ascii=False)};
const SHOPS_BY_ACCOUNT = {json.dumps(SHOPS_BY_ACCOUNT, ensure_ascii=False)};
const CHECKED_ACCOUNTS = {json.dumps(CHECKED_ACCOUNTS)};
const PICKED = {json.dumps(PICKED)};

// === 与 SchedulesPage.selectedSites 同一逻辑 ===
const sites = new Set();
for (const accId of CHECKED_ACCOUNTS) {{
  const accShops = SHOPS_BY_ACCOUNT[accId] || [];
  const picked = PICKED[accId] || [];
  for (const shop of accShops) {{
    if (!picked.includes(shop.shopId)) continue;
    const site = String(shop.site || "").trim().toUpperCase();
    if (site) sites.add(site);
  }}
}}

// === 与 templateOptions 同一逻辑 ===
const opts = sites.size === 0
  ? TEMPLATES
  : (() => {{
      const m = TEMPLATES.filter(t => t.site === "" || sites.has(t.site));
      return m.length > 0 ? m : TEMPLATES;
    }})();

document.getElementById('tplbox').innerHTML = opts.map((t, i) => `
  <button data-tpl="${{t.name}}" aria-pressed="${{i===0}}"
    class="flex w-full items-center justify-center gap-1 rounded-md border px-1 py-1.5 text-xs ${{i===0?'border-brand-600 bg-brand-50 font-medium text-brand-700':'border-border text-fg-secondary'}}">
    <span class="min-w-0 truncate">${{t.name}}</span>
  </button>`).join('');
</script></body></html>"""

path = pathlib.Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\tools\source_match\_tpl_multi.html")
path.write_text(html, encoding="utf-8")

from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 640, "height": 300})
    pg.goto(path.as_uri())
    pg.wait_for_timeout(200)
    shown = pg.evaluate("""() => [...document.querySelectorAll('[data-tpl]')].map(b => b.dataset.tpl)""")
    b.close()

print("勾选账号: ['9' 梁静]")
print("其下店铺: LJ&shop.(MY) + LJ&shop.(PH)")
print(f"渲染模板: {shown}")
print()
ok = set(shown) == {"马来", "菲律宾"}
print("结果:", "通过 —— 只有马来和菲律宾" if ok
      else f"失败 —— 多出 {set(shown) - {'马来', '菲律宾'}}")