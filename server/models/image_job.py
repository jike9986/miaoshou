"""图片翻译任务。id = sha256(原图 + model + prompt + size)，天然幂等。

费用上限检查在写事务内做：SUM(reserved_milliyuan) 不得超过 10000（10 元）。
"""
from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.utils import now_ts
from db.session import Base


class ImageJob(Base):
    __tablename__ = "image_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(32), default="")
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL")
    )
    detail_id: Mapped[str] = mapped_column(String(32), default="")
    # main / detail / sku
    source: Mapped[str] = mapped_column(String(8), default="main")
    source_url: Mapped[str] = mapped_column(Text, default="")
    source_sha256: Mapped[str] = mapped_column(String(64), default="")
    # queued / submitted / ready / unknown / failed
    status: Mapped[str] = mapped_column(String(16), default="queued")
    model: Mapped[str] = mapped_column(String(32), default="gpt-image-2")
    prompt: Mapped[str] = mapped_column(Text, default="")
    size: Mapped[str] = mapped_column(String(8), default="1:1")
    reserved_milliyuan: Mapped[int] = mapped_column(Integer, default=24)
    task_id: Mapped[str | None] = mapped_column(String(128))
    upstream_status: Mapped[str] = mapped_column(String(32), default="")
    filename: Mapped[str] = mapped_column(String(80), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    retry_of: Mapped[str | None] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    result_url: Mapped[str] = mapped_column(Text, default="")
    # 图床公网地址。result_url 只是本地相对路径（/api/v1/images/jobs/{id}/file），
    # 上游平台和前端导出都拿不到；入仓写回 payload 时又必须给公网 https 地址，
    # 此前每次写回都现传一遍（warehouse_service._PUBLIC_URL_CACHE 只能救同进程）。
    # 落库后重试、导出、跨进程复用都不用再传。
    public_url: Mapped[str] = mapped_column(Text, default="")
    # 上传成功时间。空表示还没传成功；与 public_url 一起构成「传没传过」的判据。
    uploaded_at: Mapped[float | None] = mapped_column(Float)
    # 上传失败原因。上传失败绝不改 status：图已经生成、钱已经花了，
    # 失败的只是「传到图床」这一步，必须留着让操作者重试。
    upload_error: Mapped[str] = mapped_column(String(500), default="")
    # 谁提交的这次翻译。图片翻译按张扣费，一张 24 毫（0.024 元），
    # 一批 200 张就是 4.8 元——月底「这个月图片花了多少钱、谁花的」
    # 只有这一列答得出来。同 products/batches/publish_logs：
    # 可空、不建外键、不建索引，才能让 _add_missing_columns 直接 ADD COLUMN。
    operator_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float, default=now_ts)
    updated_at: Mapped[float] = mapped_column(Float, default=now_ts, onupdate=now_ts)
    ready_at: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        Index("ix_image_jobs_status_time", "status", "created_at"),
        Index("ix_image_jobs_product", "product_id"),
        Index("ix_image_jobs_batch", "batch_id"),
    )
