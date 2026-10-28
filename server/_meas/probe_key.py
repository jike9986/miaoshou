import os, sys
sys.path.insert(0, os.getcwd())
# 故意指向一个没有 image-key.txt 的目录，逼它走回退
os.environ["AOHUANG_DATA_DIR"] = r"E:\codex工作台\p006\tk-desktop\_bt\empty_data"
os.makedirs(os.environ["AOHUANG_DATA_DIR"], exist_ok=True)
from core.config import get_settings
s = get_settings()
key = s.image_api_key()
print("回退取到密钥长度:", len(key), "前缀:", key[:3] if key else "(空)")
assert len(key) > 0, "回退失败"
print("[OK] 老版密钥回退生效")
