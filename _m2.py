from pathlib import Path
import paramiko, time

PASSWORD_FILE = Path("C:/Users/Administrator/Desktop/\u96e8\u4e91.txt")
pw = [l.strip() for l in PASSWORD_FILE.read_text(encoding="utf-8").splitlines() if l.strip()][-1]
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("38.95.75.47", 22, "root", pw, timeout=30, allow_agent=False, look_for_keys=False)
print("等待 180 秒…")
time.sleep(180)

probe = '''
import sys
sys.path.insert(0, "/opt/miaoshou-listing-v2/server")
from core.config import get_settings
from sqlalchemy import create_engine, text
eng = create_engine(str(get_settings().database_url))
with eng.connect() as conn:
    print("=== 全部发布任务 ===")
    for r in conn.execute(text("""
        SELECT id, status,
               COALESCE(settings->'progress'->>'shopIndex','0') AS idx,
               COALESCE(settings->'progress'->>'shopTotal','0') AS tot,
               left(COALESCE(errors::text,'[]'),120) AS err
        FROM batches WHERE COALESCE(settings->>'kind','') = 'publish_job'
          AND created_at > extract(epoch from now()) - 3600
        ORDER BY created_at DESC
    """)).fetchall():
        d = dict(r._mapping)
        print(f"  {d['id']} {d['status']:10s} {d['idx']}/{d['tot']}  err={d['err'][:80]}")
    print()
    print("=== 明细汇总（近1小时任务）===")
    for r in conn.execute(text("""
        SELECT i.status, count(*) AS n
        FROM batch_items i JOIN batches b ON b.id=i.batch_id
        WHERE COALESCE(b.settings->>'kind','')='publish_job'
          AND b.created_at > extract(epoch from now()) - 3600
        GROUP BY i.status ORDER BY 2 DESC
    """)).fetchall():
        print("  ", dict(r._mapping))
'''
sftp = c.open_sftp()
with sftp.open("/tmp/_m2.py", "w") as f:
    f.write(probe)
sftp.close()
_, out, err = c.exec_command(
    "cd /opt/miaoshou-listing-v2/server && set -a && . /etc/aohuang-listing-v2.env && set +a && "
    "timeout 120 /opt/miaoshou-listing-v2/venv/bin/python /tmp/_m2.py 2>&1 | tail -22", timeout=200)
print(out.read().decode("utf-8", "replace"))
e = err.read().decode("utf-8", "replace")
if e.strip(): print("ERR:", e[:300])
c.close()
