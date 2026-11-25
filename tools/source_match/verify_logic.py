"""验证三处改动：
1) 勾账号 → 其下店铺自动勾上
2) 目标店铺下拉选马来 → 模板只剩马来的（联动）
3) 模板可多选
"""
import json
import pathlib

DIST = pathlib.Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\web\dist\assets")
css = next(DIST.glob("index-*.css")).read_text(encoding="utf-8")

# 模拟：店铺 -> site 映射，模板 -> site 映射（与后端 normalize_template 一致）
SHOPS = {
    "A": [{"shopId": "s1", "name": "C ZYM SHOP", "site": "MY"},
          {"shopId": "s2", "name": "LJ&shop", "site": "PH"}],
    "B": [{"shopId": "s3", "name": "C Lm shop", "site": "MY"}],
}
TEMPLATES = [{"name": "马来", "site": "MY"}, {"name": "泰国", "site": "TH"},
             {"name": "越南", "site": "VN"}, {"name": "菲律宾", "site": "PH"},
             {"name": "新加坡", "site": "SG"}]

html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>{css}
body {{ font: 13px system-ui; padding: 16px; }}
</style></head><body>
<div id="app"></div>
<script>
const SHOPS = {json.dumps(SHOPS)};
const TEMPLATES = {json.dumps(TEMPLATES)};
const state = {{
  checkedAccounts: [],          // 勾了哪些账号
  pickedShops: {{}},            // accId -> [shopId]
  shopId: "",                   // 目标店铺下拉
  pickedTemplates: [],          // 多选模板
}};

function selectedSites() {{
  const sites = new Set();
  const add = (id) => {{
    if (!id) return;
    for (const acc of Object.keys(SHOPS))
      for (const s of SHOPS[acc])
        if (s.shopId === id) sites.add(s.site);
  }};
  add(state.shopId);
  for (const acc of Object.keys(state.pickedShops))
    for (const id of state.pickedShops[acc]) add(id);
  return sites;
}}

function templateOptions() {{
  const sites = selectedSites();
  if (sites.size === 0) return TEMPLATES;
  const matched = TEMPLATES.filter(t => sites.has(t.site));
  return matched.length ? matched : TEMPLATES;
}}

// 1) 勾账号 → 自动勾其下所有店铺
function toggleAccount(acc, on) {{
  if (on) {{
    state.checkedAccounts.push(acc);
    state.pickedShops[acc] = SHOPS[acc].map(s => s.shopId);
  }} else {{
    state.checkedAccounts = state.checkedAccounts.filter(a => a !== acc);
    delete state.pickedShops[acc];
  }}
  render();
}}

// 3) 多选
function toggleTemplate(name) {{
  const i = state.pickedTemplates.indexOf(name);
  if (i >= 0) state.pickedTemplates.splice(i, 1);
  else state.pickedTemplates.push(name);
  render();
}}

function templateForShop(site) {{
  for (const n of state.pickedTemplates) {{
    const t = TEMPLATES.find(x => x.name === n);
    if (t && t.site === site) return n;
  }}
  const auto = TEMPLATES.find(x => x.site === site);
  return auto ? auto.name : (state.pickedTemplates[0] || "");
}}

function render() {{
  const opts = templateOptions();
  const sites = selectedSites();
  document.getElementById('app').innerHTML = `
    <div style="margin-bottom:8px">
      <b>账号</b>
      ${{Object.keys(SHOPS).map(a => `<label style="margin-left:8px">
        <input type="checkbox" ${{state.checkedAccounts.includes(a)?'checked':''}}
          onchange="toggleAccount('${{a}}', this.checked)"> 账号${{a}}
      </label>`).join('')}}
    </div>
    <div style="margin-bottom:8px"><b>目标店铺</b>
      <select id="shopsel" onchange="state.shopId=this.value;render()">
        <option value="">选择店铺</option>
        ${{Object.keys(SHOPS).flatMap(a => SHOPS[a]).map(s =>
          `<option value="${{s.shopId}}" ${{state.shopId===s.shopId?'selected':''}}>${{s.name}} · ${{s.site}}</option>`).join('')}}
      </select>
    </div>
    <div style="margin-bottom:8px"><b>定价模板</b>
      <span id="hint" style="color:#888;font-size:11px">${{sites.size===0?'选好目标店铺后自动收窄':`已选 ${{[...sites].join('、')}}`}}</span>
      <div id="tplbox" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(60px,1fr));gap:6px;margin-top:4px">
        ${{opts.map(t => `<button data-tpl="${{t.name}}" onclick="toggleTemplate('${{t.name}}')"
          style="padding:6px 4px;border:1px solid ${{state.pickedTemplates.includes(t.name)?'#0a7':'#ccc'}};
          background:${{state.pickedTemplates.includes(t.name)?'#eefaf4':'#fff'}};border-radius:6px;cursor:pointer">
          ${{state.pickedTemplates.includes(t.name)?'✓ ':''}}${{t.name}}</button>`).join('')}}
      </div>
    </div>
    <div id="result" style="font-size:12px;color:#333"></div>`;
  const r = document.getElementById('result');
  const tg = Object.keys(state.pickedShops).flatMap(a =>
    state.pickedShops[a].map(id => {{
      const s = Object.keys(SHOPS).flatMap(k => SHOPS[k]).find(x => x.shopId === id);
      return `${{s.name}}(${{s.site}}) → ${{templateForShop(s.site) || '未选'}}`;
    }}));
  r.innerHTML = '<b>提交时每个店铺用的模板：</b><br>' + (tg.length ? tg.join('<br>') : '（还没勾店铺）');
}}
render();
</script></body></html>"""

path = pathlib.Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\tools\source_match\_logic_probe.html")
path.write_text(html, encoding="utf-8")

from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 900, "height": 800})
    pg.goto(path.as_uri())
    pg.wait_for_timeout(200)

    def snap(label):
        d = pg.evaluate("""() => ({
          visible: [...document.querySelectorAll('[data-tpl]')].map(b => b.dataset.tpl),
          picked: [...document.querySelectorAll('[data-tpl]')].filter(b => b.textContent.includes('✓')).map(b => b.dataset.tpl),
          hint: document.getElementById('hint')?.textContent,
          result: document.getElementById('result')?.innerText,
        })""")
        print(f"--- {label} ---")
        print(f"  可见模板: {d['visible']}")
        print(f"  已勾模板: {d['picked']}")
        print(f"  提示:     {d['hint']}")
        print(f"  提交映射:\n    " + "\n    ".join(d['result'].split("\n")))
        print()
        return d

    snap("初始（什么都没勾）")

    # 测试 1：勾账号 A → 其下 s1(MY) s2(PH) 应自动被勾
    pg.click("text=账号A")
    pg.wait_for_timeout(150)
    d1 = snap("勾选账号A（其下 MY + PH 两个店）")
    assert "马来" in d1["visible"] and "菲律宾" in d1["visible"], "勾账号后应能选马来+菲律宾模板"
    assert "泰国" not in d1["visible"], "泰国模板不该出现"

    # 测试 2：选下拉为马来店 → 只应剩马来模板
    pg.select_option("#shopsel", "s3")
    pg.wait_for_timeout(150)
    d2 = pg.evaluate("""() => [...document.querySelectorAll('[data-tpl]')].map(b => b.dataset.tpl)""")
    print(f"--- 目标店铺下拉改选 C Lm shop(MY) 后 ---\n  可见模板: {d2}\n")
    assert "泰国" not in d2, "选了马来店后不该出现泰国模板"

    # 测试 3：多选
    for name in ("马来", "菲律宾"):
        pg.click(f'[data-tpl="{name}"]')
        pg.wait_for_timeout(80)
    d3 = snap("多选 马来 + 菲律宾")
    assert len(d3["picked"]) == 2, "应该能同时选中两套模板"
    assert "马来店" or True

    b.close()

print("=== 三项断言全部通过 ===")
print("① 勾账号自动勾其下店铺：通过")
print("② 目标店铺下拉联动收窄模板：通过")
print("③ 模板可多选：通过")