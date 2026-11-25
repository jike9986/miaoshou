"""测量发布弹窗在 640px(lg) 下账号网格三列的实际宽度。

不靠算术推算：用构建产物 CSS + 真实 DOM 结构跑 Playwright，
拿 getBoundingClientRect / scrollWidth 判断是否挤压或溢出。
"""
import pathlib

DIST = pathlib.Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\web\dist\assets")
css = next(DIST.glob("index-*.css")).read_text(encoding="utf-8")

# 与 PublishDialog 真实结构一致：Dialog lg=640 内容 px-6（左右各 24）
html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>{css}</style></head><body style="margin:0;background:#eee">
<div style="width:640px;margin:20px auto;background:#fff;border-radius:10px;
            box-shadow:0 8px 30px rgba(0,0,0,.12);padding:16px 24px;
            font:13px system-ui;box-sizing:border-box">
  <p style="font-weight:600;margin:0 0 6px">发布账号</p>
  <div style="max-height:224px;overflow-y:auto;border:1px solid #e5e7eb;
              border-radius:6px;padding:8px;box-sizing:border-box">
    <div id="grid" class="grid grid-cols-2 gap-x-2 gap-y-1 sm:grid-cols-3">
      <!-- 12 个账号，模拟真实数量 -->
    </div>
  </div>
</div>
<script>
const names = ["分销","蒋新宇","赖彩红","罗蔓","妙手项敏子","吴丽玲",
               "奚佳玥","梁静","妙手吴佳芮","妙手王薪楷","黄映明","周月婧"];
document.getElementById('grid').innerHTML = names.map((n, i) => `
  <div class="rounded-md border border-border-subtle bg-canvas-inset/40 px-2 py-1.5"
       data-cell>
    <label class="flex cursor-pointer items-center gap-2 text-[13px]">
      <input type="checkbox" class="h-4 w-4 rounded border-border accent-brand-500"
             ${{i === 7 ? 'checked' : ''}}>
      <span class="truncate font-medium text-fg">${{n}}</span>
    </label>
    ${{i === 7 ? `<div class="mt-2 space-y-1">
        <label class="flex cursor-pointer items-center gap-2 text-xs">
          <input type="checkbox" class="h-3 w-3 rounded border-border accent-brand-500" checked>
          <span class="truncate text-fg-secondary">LJ&shop.
            <span class="ml-1 text-fg-muted">· 马来</span></span>
        </label>
        <label class="flex cursor-pointer items-center gap-2 text-xs">
          <input type="checkbox" class="h-3 w-3 rounded border-border accent-brand-500" checked>
          <span class="truncate text-fg-secondary">LJ&shop.
            <span class="ml-1 text-fg-muted">· 菲律宾</span></span>
        </label>
      </div>` : ''}}
  </div>`).join('');
</script></body></html>"""

p = pathlib.Path(r"D:\缓存文件夹\codex工作台\p006\miaoshou\tools\source_match\_pub_grid.html")
p.write_text(html, encoding="utf-8")

from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1000, "height": 600})
    pg.goto(p.as_uri())
    pg.wait_for_timeout(200)

    d = pg.evaluate("""() => {
      const grid = document.getElementById('grid');
      const cells = [...document.querySelectorAll('[data-cell]')];
      const box = grid.getBoundingClientRect();
      const style = getComputedStyle(grid);
      const cols = style.gridTemplateColumns.split(' ').length;
      // 每个 cell 宽度 + 是否有内容被截断
      const widths = cells.map(c => Math.round(c.getBoundingClientRect().width));
      const truncated = cells.filter(c => {
        const s = c.querySelector('span.truncate');
        return s && s.scrollWidth > s.clientWidth + 1;
      }).map(c => c.querySelector('span.truncate').textContent.trim());
      return {
        gridWidth: Math.round(box.width),
        columns: cols,
        colTemplate: style.gridTemplateColumns,
        cellWidths: [...new Set(widths)],
        overflowX: grid.scrollWidth > grid.clientWidth + 1,
        truncatedNames: truncated,
      };
    }""")

    print("=== 发布弹窗 640px · 账号网格 ===")
    print(f"  网格宽度:     {d['gridWidth']}px")
    print(f"  列数:         {d['columns']}")
    print(f"  列宽:         {d['colTemplate']}")
    print(f"  单元格宽度:   {d['cellWidths']}")
    print(f"  横向溢出:     {d['overflowX']}")
    print(f"  名字被截断的: {d['truncatedNames'] or '无'}")
    print()
    ok = d["columns"] == 3 and not d["overflowX"] and not d["truncatedNames"]
    print("结果:", "✓ 三列，无溢出、无截断" if ok else "✗ 需要调整")

    pg.screenshot(path=str(p.with_suffix(".png")), clip={"x": 150, "y": 0, "width": 700, "height": 500})
    b.close()