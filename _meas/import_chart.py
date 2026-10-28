"""把老项目的尺码图搬进新库。

老项目两处来源：
  1. config/size-charts.json  -> 只有一条，是 translation-temp/chart.png（已 404，废弃）
  2. src/defaults.py 里的 DEFAULT_SIZE_CHART_URL -> size-chart.png（200，1.2MB，就是这张）

新库表 size_charts 需要 (name, site, url)。site 用空串表示「通用」，
唯一约束是 (name, site)，所以重复导入会被 upsert 挡住。
"""

import sqlite3
import time

DEFAULT_CHART = "https://aohuang888.com/tk-product-automation/size-chart.png"
BROKEN_CHART = "translation-temp/chart.png"
NAME = "项目默认尺码图"

DBS = [
    r"E:/codex工作台/p006/tk-desktop/.runtime-data/app.db",
    r"C:/AohuangDesktop/userdata/app.db",
]

for db in DBS:
    try:
        conn = sqlite3.connect(db)
        # 先清掉之前导错的 404 记录
        conn.execute("DELETE FROM size_charts WHERE url LIKE ?", ("%" + BROKEN_CHART,))
        existing = conn.execute(
            "SELECT id FROM size_charts WHERE url = ?", (DEFAULT_CHART,)
        ).fetchone()
        if existing:
            print(f"{db}: 已存在 id={existing[0]}，跳过")
        else:
            now = time.time()
            conn.execute(
                "INSERT INTO size_charts (name, site, url, content_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (NAME, "", DEFAULT_CHART, None, now, now),
            )
            print(f"{db}: 已导入默认尺码图")
        conn.commit()
        rows = conn.execute("SELECT id, name, url FROM size_charts").fetchall()
        print(f"  当前 {len(rows)} 条: {rows}")
    except Exception as exc:
        print(f"{db}: ERR {exc}")
