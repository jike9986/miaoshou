"""妙手草稿的处理标记：文字 / 图片各自记一次「这件已经处理过了」。

## 为什么要单独一张表，而不是挂在 `products` 上

因为 `products` 的行**只有入仓成功才会存在**。而「文字处理」是在写回妙手
草稿那一步就完成的，比入仓早得多。2026-11-27 实测的 6 件商品就是活证据：

    detailId=3563357906  库中无 products 行，但草稿里的颜色已经是马来语
    detailId=3563357863  同上
    …6 件全部如此

也就是说：文字早就处理并写回草稿了，只是后面的读回核验判了不一致，
整件回滚，`products` 里连一行都没留下 —— 标记挂在那里必然跟着丢。
挂到草稿身份上（account + detail + shop）才是唯一稳的位置。

## 两个标记各自的含义

`text_done`   —— 中文阶段的清洗与翻译已经做完并写回妙手草稿。
                 颜色词表、尺码清洗、违规词表全是按**中文**写的：
                 拿这些规则去撞已经是马来语的草稿，`Meter`（马来语"米色"）
                 认不出会被当成厂家广告词删掉，两个颜色并成一个，
                 读回核验立刻报 `skuImages` 不一致。所以处理过就必须跳过。

`image_done`  —— 图片 OCR 筛选、翻译、写回草稿已经做完。
                 图片是按张计费的（一张 24 毫 = 0.024 元），
                 重复处理等于重复扣费，且会把已经译好的图再提交一遍。

## 与 `products.automation_images` 的分工（不重复）

`automation_images` 记的是**当前这一次**图片任务的生命周期
（queued / submitted / failed / generation），每次处理都会重置，
是运行态。本表记的「这件草稿处理完成过」是**跨次持久态**，
两者不冲突：一个回答"这次跑到哪了"，一个回答"这件还要不要再跑"。
"""
from __future__ import annotations

from sqlalchemy import Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class DraftMark(Base):
    """一件妙手草稿的处理进度。一行 = 一件草稿。"""

    __tablename__ = "draft_marks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    detail_id: Mapped[str] = mapped_column(String(32), nullable=False)
    shop_id: Mapped[str] = mapped_column(String(32), default="")

    # 文字处理完成。写回妙手草稿成功后立刻置位，**独立事务提交**，
    # 不随之后的读回核验失败一起回滚 —— 否则读回一失败标记就没了，
    # 下次又拿中文规则洗一遍马来语草稿，正是 2026-11-27 那个死循环。
    text_done: Mapped[bool] = mapped_column(default=False)
    text_at: Mapped[float | None] = mapped_column(Float)
    # 写回时用的目标语言。换语言处理时标记必须失效（重新翻一遍），
    # 没有这一列就只能靠人记住上次选的什么语言。
    text_language: Mapped[str] = mapped_column(String(16), default="")

    # 图片处理完成。全部图片任务 ready 且写回草稿后才置位。
    image_done: Mapped[bool] = mapped_column(default=False)
    image_at: Mapped[float | None] = mapped_column(Float)
    # 这次图片处理覆盖了几个待译格子（main/sku/detail 之和）。
    # 数量变了说明商品图换了，旧标记不能直接复用。
    image_total: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[float] = mapped_column(Float, default=now_ts)
    updated_at: Mapped[float] = mapped_column(Float, default=now_ts, onupdate=now_ts)

    __table_args__ = (
        UniqueConstraint("account_id", "detail_id", "shop_id",
                         name="uq_draft_marks_identity"),
        # 处理流程每次都按这三个字段定位，主查询必须走索引。
        Index("ix_draft_marks_lookup", "account_id", "detail_id", "shop_id"),
    )
