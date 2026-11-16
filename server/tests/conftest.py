"""pytest 配置：确保 server/ 目录在 sys.path 中。"""
import sys
from pathlib import Path

# server/ 目录加入 path，使 `from services import ...` 能直接 import
SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))
