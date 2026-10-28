import sys, os, json, time, tempfile, hashlib
sys.path.insert(0, os.getcwd())
from pathlib import Path
from services import prune_service

root = Path(tempfile.mkdtemp())
(root / "image-cache").mkdir(); (root / "image-results").mkdir()
# 记录类文件
(root / "run-records.json").write_text('{"a":1}', encoding="utf-8")
(root / "image-generation-ledger.json").write_text('{}', encoding="utf-8")
# 一个被记录引用的哈希
digest = hashlib.sha256(b"x").hexdigest()
(root / "run-records.json").write_text(json.dumps({"url": "https://x/t/%s.png" % digest}), encoding="utf-8")
ref = root / "image-results" / (digest + ".png"); ref.write_text("img")
# 一个没被引用的旧文件
lonely = root / "image-cache" / "old.png"; lonely.write_text("abc")
# 一个新文件（不该删）
fresh = root / "image-cache" / "new.png"; fresh.write_text("xyz")
old = time.time() - 30*86400
for p in (ref, lonely):
    os.utime(p, (old, old))

r1 = prune_service.prune(root, 7, confirm=False)
print("=== 只出报告 ===")
print(json.dumps({k: v for k, v in r1.items() if k != "deletable"}, ensure_ascii=False, indent=1))
print("文件还在:", lonely.exists(), ref.exists())

r2 = prune_service.prune(root, 7, confirm=True)
print("=== 真删 ===")
print(json.dumps({k: v for k, v in r2.items() if k != "deletable"}, ensure_ascii=False, indent=1))
print("未被引用的旧文件已删:", not lonely.exists())
print("被记录引用的旧文件仍在:", ref.exists())
print("新文件仍在:", fresh.exists())
print("记录文件仍在:", (root/"run-records.json").exists(), (root/"image-generation-ledger.json").exists())
