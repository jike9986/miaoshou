"""模型包：导入即注册到 Base.metadata，create_all 才能建全表。"""
# ruff: noqa: I001  —— 这里的 import 顺序是**语义**，不是格式：
#   1) 导入顺序决定模型注册进 Base.metadata 的顺序，create_all 按注册顺序建表，
#      外键指向的表必须已存在，所以顺序不能交给 isort 按字母排；
#   2) 0.2 的两个新模型刻意追加在末尾（见下方注释），插进字母序会让整个文件的
#      diff 变红，而这里没有任何语义要求字母序。
# 这是本项目里唯一豁免 I001 的地方；其余文件一律走 ruff --fix 自动排序。
from __future__ import annotations

from models.account import Account
from models.automation_run import AutomationRun, AutomationRunItem
from models.batch import Batch, BatchItem
from models.category import Category
from models.group import Group
from models.image_job import ImageJob
from models.product import Product
from models.product_claim import ProductClaim
from models.publish_log import PublishLog
from models.schedule import Schedule
from models.schedule_log import ScheduleLog
from models.setting import Setting
from models.shop import Shop
from models.violation_word import ViolationWord

# 操作员 + 审计（0.2 新增）。刻意**追加在末尾而不是插进字母序**：
# 插入会让这个文件里其余每一行的 diff 都变红，而这里没有任何语义要求
# 字母序——加在末尾，review 时一眼就能看出「只多了两行」。
from models.audit_log import AuditLog
from models.operator import Operator
from models.login_rate_limit import LoginRateLimit

__all__ = [
    "Account",
    "AutomationRun",
    "AutomationRunItem",
    "Batch",
    "BatchItem",
    "Category",
    "Group",
    "ImageJob",
    "Product",
    "ProductClaim",
    "PublishLog",
    "Schedule",
    "ScheduleLog",
    "Setting",
    "Shop",
    "ViolationWord",
    # 0.2 追加
    "AuditLog",
    "Operator",
    "LoginRateLimit",
]
