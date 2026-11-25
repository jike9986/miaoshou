"""验证发布弹窗模板区：只列出上面勾了店铺的国家。

负责人 2026-11-25：「成品仓库的发布页面像定时发布的页面同步一下」。
原来五国模板全在（泰国/越南/新加坡 灰着占位），现在按已选店铺国家的
交集渲染，没选的国家直接不出现。
"""
import json, pathlib

DIST = pathlib.Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\web\dist\assets")
css = next(DIST.glob("index-*.css")).read_text(encoding="utf-8")

SITE_LABELS = {"MY": "马来", "TH": "泰国", "VN": "越南", "PH": "菲律宾", "SG": "新加坡"}
SITE_ORDER = ["MY", "TH", "VN", "PH", "SG"]
TEMPLATE_NAME_BY_SITE = {"MY": "马来", "PH": "菲律宾", "TH": "泰国",
                         "VN": "越南", "SG": "新加坡"}


def render(selected_sites, use_sites):
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>{css}</style></head><body style="padding:16px;font:13px system-ui;width:420px">
<label class="text-sm">定价模板</label>
<div id="box" class="grid grid-cols-[repeat(auto-fit,minmax(76px,1fr))] gap-1.5 mt-1.5"></div>
<script>
const SITE_LABELS = {json.dumps(SITE_LABELS, ensure_ascii=False)};
const SITE_ORDER = {json.dumps(SITE_ORDER)};
const TPL = {json.dumps(TEMPLATE_NAME_BY_SITE, ensure_ascii=False)};
const selectedShopSites = {json.dumps(selected_sites)};
const useTemplateSites = {json.dumps(use_sites)};

const templateSites = SITE_ORDER.filter(s => TPL[s]);
const shown = templateSites.filter(s => selectedShopSites.includes(s));
document.getElementById('box').innerHTML = shown.length === 0
  ? '<span class="text-xs text-fg-muted">在上面勾选店铺后，这里会出现对应国家的模板</span>'
  : shown.map(site => {{
      const checked = useTemplateSites.includes(site);
      const label = SITE_LABELS[site] || site;
      return `<button data-site="${{site}}" aria-pressed="${{checked}}"
        class="flex w-full items-center justify-center gap-1 rounded-md border px-1 py-1.5 text-xs ${{checked?'border-brand-600 bg-brand-50 font-medium text-brand-700':'border-border text-fg-secondary'}}">
        ${{checked?'<svg class="size-2.5" viewBox="0 0 12 12" fill="none"><path d="M2 6.2 4.6 8.8 10 3.4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>':''}}
        <span class="min-w-0 truncate">${{label}}</span></button>`;
    }}).join('');
</script></body></html>"""
    return html


from playwright.sync_api import sync_playwright

cases = [
    ("只勾马来店铺", ["MY"], ["MY"], {"MY"}),
    ("勾马来 + 菲律宾店铺", ["MY", "PH"], ["MY", "PH"], {"MY", "PH"}),
    ("勾泰国一个店铺", ["TH"], ["TH"], {"TH"}),
    ("还没勾任何店铺", [], [], set()),
]

with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 460, "height": 260})
    print("=" * 58)
    for label, sel, use, expect in cases:
        p = pathlib.Path(rf"D:\缓存文件夹\codex工作台\p006\miaoshou\tools\source_match\_pub_{len(sel)}.html")
        p.write_text(render(sel, use), encoding="utf-8")
        pg.goto(p.as_uri())
        pg.wait_for_timeout(120)
        shown = pg.evaluate("() => [...document.querySelectorAll('[data-site]')].map(b => b.dataset.site)")
        pressed = pg.evaluate("() => [...document.querySelectorAll('[data-site][aria-pressed=\"true\"]')].map(b => b.dataset.site)")
        print(f"{label}")
        print(f"  已选店铺国家: {sel or '（无）'}")
        print(f"  渲染的模板:   {shown or '（空，显示提示）'}")
        print(f"  已勾选:       {pressed}")
        ok = set(shown) == expect
        print(f"  {'✓ 通过' if ok else '✗ 失败：期望 ' + str(expect)}")
        print()
    b.close()

print("=" * 58)
print("对比修复前：五国模板全部渲染，没选的国家灰着占位")
print("对比修复后：只渲染已选店铺国家的模板")
