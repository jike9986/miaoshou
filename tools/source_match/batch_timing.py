"""统计那批 20 件的处理耗时。"""
import re
import subprocess
from collections import OrderedDict

out = subprocess.run(
    ["journalctl", "-u", "aohuang-listing-v2", "--since", "90 min ago",
     "--no-pager"],
    capture_output=True, text=True).stdout

pat = re.compile(r"慢请求 POST /api/v1/collect-box/(\d+)/process (\d+)ms")
rows = []
for line in out.splitlines():
    m = pat.search(line)
    if m:
        rows.append((m.group(1), int(m.group(2))))

latest: OrderedDict[str, int] = OrderedDict()
for did, ms in rows:
    latest[did] = ms

vals = sorted(latest.values())
print(f"共 {len(vals)} 件有 process 记录")
if vals:
    print(f"  最快 {min(vals)/1000:.1f}s   最慢 {max(vals)/1000:.1f}s")
    print(f"  中位 {vals[len(vals)//2]/1000:.1f}s   平均 {sum(vals)/len(vals)/1000:.1f}s")
    print()
    print("逐件：")
    for did, ms in latest.items():
        print(f"  {did}  {ms/1000:>6.1f}s")
