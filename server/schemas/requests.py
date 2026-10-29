"""请求模型（全部 camelCase 对外，字段约束与 docs/api.md 对齐）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from schemas.common import CamelModel

ImageSource = Literal["main", "detail", "sku"]
ImageSize = Literal["1:1", "16:9", "9:16"]
GroupColor = Literal["slate", "rose", "amber", "emerald", "sky", "violet"]
WordAction = Literal["add", "remove", "import", "remove-many", "clear", "enable", "disable"]
WordCategory = Literal["remove"]


class ConnectRequest(CamelModel):
    accountId: str | None = None
    studentOperatorId: str | None = None
    name: str | None = Field(default=None, max_length=60)
    appId: str | None = Field(default=None, max_length=256)
    appSecret: str | None = Field(default=None, max_length=512)
    save: bool = True
    autoScan: bool = True


class ClaimRequest(CamelModel):
    targetShopId: str
    accountId: str | None = None
    markCollected: bool = True


class CategoryTarget(CamelModel):
    """改类目/分组的公共字段。"""

    categoryId: str | None = None
    cid: str | None = None
    name: str | None = Field(default=None, max_length=255)
    groupId: str | None = None
    group: str | None = Field(default=None, max_length=64)


class ProductPatch(CategoryTarget):
    title: str | None = None
    # 乐观锁：前端把读到商品时拿到的 `revision` 原样带回来。
    # 不传 = 不校验（兼容老调用方），但也就等于放弃了这层保护。
    expectedRevision: int | None = None


class ProductQualityReviewRequest(CamelModel):
    weightKg: float = Field(gt=0, le=1000)
    packageLength: float = Field(gt=0, le=10000)
    packageWidth: float = Field(gt=0, le=10000)
    packageHeight: float = Field(gt=0, le=10000)
    source: str = Field(min_length=1, max_length=120)
    evidence: str = Field(min_length=3, max_length=1000)
    expectedRevision: int = Field(ge=1)

    @field_validator("source", "evidence", mode="before")
    @classmethod
    def trim_review_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class BatchCategoryRequest(CategoryTarget):
    ids: list[str] = Field(default_factory=list)


class BatchDeleteRequest(CamelModel):
    ids: list[str] = Field(default_factory=list)


class BatchClaimRequest(CamelModel):
    keys: list[str] = Field(default_factory=list)
    # 撤销认领（claimed=false）不需要目标店铺，所以这里是可选的；
    # 认领路径由路由层强制校验非空，报错文案才指向「请选择认领到哪个店铺」。
    targetShopId: str = ""
    accountId: str | None = None
    claimed: bool = True


class GroupCreate(CamelModel):
    name: str = Field(max_length=64)
    color: GroupColor | None = None
    sort: int | None = Field(default=None, ge=0, le=999)


class GroupPatch(CamelModel):
    name: str | None = Field(default=None, max_length=64)
    color: GroupColor | None = None
    sort: int | None = Field(default=None, ge=0, le=999)


class CategorySyncRequest(CamelModel):
    site: str | None = None
    accountId: str | None = None
    rootCid: str | None = None


class ImageItem(CamelModel):
    productId: str | None = None
    detailId: str | None = None
    source: ImageSource
    url: str | None = None
    imageBase64: str | None = None
    size: ImageSize | None = None
    # 人工核对的结论（老版 image-plan 的 rows[].action）：
    # keep 跳过不译、translate 要译；不传＝按识别结果照做。
    # 界面上逐张改的就是这个字段。
    action: Literal["keep", "translate"] | None = None


class ImageTranslateRequest(CamelModel):
    batchId: str | None = Field(default=None, max_length=32)
    size: ImageSize = "1:1"
    retryOcr: bool = False
    items: list[ImageItem] = Field(default_factory=list)


class PublishItem(CamelModel):
    warehouseId: str | None = None
    detailId: str | None = None
    shopId: str | None = None


class PublishRequest(CamelModel):
    items: list[PublishItem] = Field(default_factory=list)
    shopId: str
    accountId: str | None = None
    template: str | None = None
    exchangeRate: str | float | None = None
    language: str | None = None
    allowMismatch: bool = False
    confirm: bool = False
    claimOnly: bool = False
    titles: dict[str, str] = Field(default_factory=dict)
    scheduleId: str | None = None
    batchId: str | None = None


class ScheduleCreate(CamelModel):
    name: str | None = Field(default=None, max_length=120)
    enabled: bool = True
    accountId: str
    shopId: str
    template: str
    # 用 str 而不是 Literal：Literal 会在 Pydantic 层先拒掉，
    # 用户看到的是英文枚举清单。交给 schedule_service 校验，
    # 才能报「频率只能是每天 / 每周 / 每月 / 指定日期」。
    frequency: str = "daily"
    days: list[int] = Field(default_factory=list)
    dayOfMonth: list[int] = Field(default_factory=list)
    startDate: str = ""
    endDate: str = ""
    time: str = "09:00"
    perRun: int = Field(default=10, ge=1, le=200)
    claimOnly: bool = False


class SchedulePatch(CamelModel):
    name: str | None = Field(default=None, max_length=120)
    enabled: bool | None = None
    accountId: str | None = None
    shopId: str | None = None
    template: str | None = None
    frequency: str | None = None
    days: list[int] | None = None
    dayOfMonth: list[int] | None = None
    startDate: str | None = None
    endDate: str | None = None
    time: str | None = None
    perRun: int | None = Field(default=None, ge=1, le=200)
    claimOnly: bool | None = None


class WordRequest(CamelModel):
    action: WordAction
    word: str | None = Field(default=None, max_length=64)
    words: list[str] | None = None
    category: WordCategory = "remove"
    mode: Literal["append", "replace"] = "append"
    confirm: bool = False


class UpdateRequest(CamelModel):
    url: str
    sha256: str
    restart: bool = True


class SettingPatch(CamelModel):
    securityReadOnly: bool | None = None
    publishEnabled: bool | None = None
    uiLastView: str | None = None


class TemplateRequest(CamelModel):
    template: dict[str, Any]


class PricePreviewRequest(CamelModel):
    """未入仓商品的定价试算入参（老版 `/api/price-preview`）。

    `purchaseCny` / `weightKg` / `rate` 刻意声明成 Any：区间与格式校验放在
    路由里做，报错文案才是中文（pydantic 的类型错误是英文），
    与 sync 那边的处理口径一致。
    """

    purchaseCny: Any = None
    weightKg: Any = None
    templateName: str | None = Field(
        default=None, description="用哪套模板；空则按四级兜底取当前选中")
    rate: Any = Field(default=None, description="CNY→站点币种汇率；空则走兜底汇率")
    site: str | None = Field(default=None, description="站点代码，仅用于回显")
    accountId: str | None = Field(default=None, description="按哪个账号的选中记录兜底")


class ProcessRequest(CamelModel):
    """处理一件商品（拉详情→定价→入仓）的附加接口。"""

    shopId: str
    accountId: str | None = None
    language: str | None = None
    groupId: str | None = None
    template: str | None = None
    exchangeRate: str | float | None = None
    save: bool = True
    # 图片处理开关（老版 process_images / imagesEnabled）。
    # 前端勾了「图片」才会提交翻译任务；不传＝不处理，保持原有行为。
    processImages: bool = False
    # 各类图片分别限制前 N 张；新字段默认 0，旧客户端仍只处理主图。
    mainImageCount: int = Field(default=5, ge=0, le=9)
    skuImageCount: int = Field(default=0, ge=0, le=9)
    detailImageCount: int = Field(default=0, ge=0, le=9)
    # 两道人工复核闸门的确认开关（老版 prepare_shoe 抛错停下等人看）。
    # 默认 False = 遇到需复核的标题/主图就拦下，确认过才放行。
    allowTitleReview: bool = False
    allowImageReview: bool = False
    # 跳过主图 OCR：批量入仓时用，一件最多 9 张、冷跑几十秒。
    skipImageReview: bool = False


class CustomCategoryRequest(CamelModel):
    """自定义分类标签的入参：加 / 删 / 改名（老版 add_custom_category）。

    只限 name（改名再带 oldName）：`name` 空串交给服务层统一报
    「分类名不能为空」，不在 pydantic 层拦成 422，前端只需处理一种错误。
    """

    name: str | None = None
    oldName: str | None = None


# --- 批次编排（老版 POST /api/start 的入参） --------------------------------
class BatchCreateRequest(CamelModel):
    """建一个批次。detailIds 去重与上限校验放在服务层，与老版一致：
    pydantic 只保证「是列表」，去重/上限的错误码由服务层统一给
    （VALIDATION_ERROR），前端只需处理一种错误形态。
    """

    accountId: str | None = None
    shopId: str = ""
    detailIds: list[str] = Field(default_factory=list)
    # 逐件处理参数，字段名与 ProcessRequest 对齐（language / template /
    # groupId / exchangeRate / save / processImages / mainImageCount）
    settings: dict[str, Any] = Field(default_factory=dict)


class BatchControlRequest(CamelModel):
    """pause / resume / stop。非法值在服务层报 VALIDATION_ERROR，
    这样「动作写错」和「状态不允许这个动作」走同一个错误码分支。"""

    action: str = ""


class BatchRetryRequest(CamelModel):
    """重试失败件。

    `allowUnknown`：上一次保存结果未知（进程崩在提交中间 / 上游超时）时，
    默认拒绝重跑——重跑可能造成第二次入仓（重量再加一次增量）。
    操作者在妙手核对过、确认没保存，才带 true 放行。
    """

    allowUnknown: bool = False


# --- 仓库图片改写（老版 POST /api/save-images） -----------------------------
class ProductImagesPatch(CamelModel):
    """改仓库里商品的主图 / 详情 / SKU 属性。

    只改本地 DB 的 payload，不碰妙手草稿——上游保存是发布环节的事，
    混在一起会让「改张图」变成一次不可撤销的上游提交。
    """

    imgUrls: list[str] | None = None
    notes: str | None = None
    skuPropertyList: list[dict[str, Any]] | None = None
    # 改图片是不可撤销的操作，必须显式确认（老版 save_images 的 confirm）
    confirm: bool = False
    # 乐观锁，语义同 ProductPatch.expectedRevision：整份 payload 会被替换，
    # 这是最容易吃掉别人改动的路径，前端必须带上版本号。
    expectedRevision: int | None = None
