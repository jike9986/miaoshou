import sqlite3

BAD = "translation-temp/chart.png"
for db in [
    r"E:/codex工作台/p006/tk-desktop/.runtime-data/app.db",
    r"C:/AohuangDesktop/userdata/app.db",
]:
    try:
        c = sqlite3.connect(db)
        c.execute("DELETE FROM size_charts WHERE url LIKE ?", ("%" + BAD,))
        c.commit()
        n = c.execute("SELECT COUNT(*) FROM size_charts").fetchone()[0]
        print(db, "剩余:", n)
    except Exception as exc:
        print(db, "ERR", exc)
