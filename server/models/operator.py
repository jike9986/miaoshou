"""操作员身份。

**这不是权限系统**，是「谁做的能查到」。运营团队共用一个装机实例，
内部彼此信任，不需要密码、角色、权限矩阵、多租户隔离——需要的只是
出事（改错价格、误删商品、发错店铺）时能回答「这一步是谁点的」。

因此这里刻意做得很薄：
  · 一个人一条记录，`name` 就是显示名（张三、李四），`token` 就是他的凭据；
  · 没有密码字段，没有角色表，没有资源-动作的权限矩阵；
  · `is_admin` 只用于「停用别人 / 改共享设置」这两件会波及全员的事，
    不是分级授权——它挡的是手滑，不是恶意。

`token` 列只保存 bearer token 的 SHA-256 摘要。新 token 的明文只在创建、登录或
重发响应里返回一次；无法找回时应重发，而不是从数据库恢复旧凭据。启动时会把
现有明文值原位转换为摘要，既有客户端无需重新登录。

时间字段统一存 Unix 秒（REAL），与 docs/api.md 0.1 约定一致。
"""
from __future__ import annotations

from sqlalchemy import Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class Operator(Base):
    __tablename__ = "operators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 显示名，如「张三」。唯一：两个人同名，审计日志里就分不清谁是谁了。
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(256))
    must_change_password: Mapped[bool] = mapped_column(
        default=False, server_default="0", nullable=False)
    # 每人一个 token 的 SHA-256 摘要，唯一；明文只在创建/登录/重发响应中出现。
    token: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # 停用（离职/换人）而不是删除：删了之后 audit_logs.operator_id 会变 NULL，
    # 只剩冗余的 operator_name 可查——停用能保住关联，也保住历史。
    enabled: Mapped[bool] = mapped_column(default=True)
    # 只用于「停用别人 / 改共享设置」，不是权限分级。见模块 docstring。
    is_admin: Mapped[bool] = mapped_column(default=False)
    # 三级角色：admin（管理员）/ teacher（老师）/ member（成员）。
    # 管理员能做所有事；老师能处理商品和发布，但不能改系统设置；
    # 成员只能看仓库和自动化，不能发布。
    # 缺省 member：老数据没有 role 字段，按最低权限处理。
    role: Mapped[str] = mapped_column(
        String(16), default="member", server_default="member", nullable=False)
    created_at: Mapped[float] = mapped_column(Float, default=now_ts)
    # 每次请求都会 touch，但节流到 60 秒一次（见 operator_service.touch）：
    # 一个批量流程能打上百个请求，不节流就是上百次 UPDATE 抢写锁。
    last_seen_at: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (Index("ix_operators_name", "name"),
                      Index("ix_operators_enabled", "enabled"))
