"""类目树：同步入库、树形查询、按 cid 找路径。

`account_id = 0` 是「全局缓存」哨兵（SQLite 唯一索引把 NULL 视为不同值）。

两级缓存：
  · 落库缓存（categories 表）——跨进程重启仍然有效，这是主缓存；
  · 进程内 TTL 缓存（``services.cache``）——``/categories`` 没命中时必须回落
    到上游拉整棵树再压平入库（1–3 秒），这一层把「短时间连点」的表现对齐
    采集箱：命中不回源，回源失败有旧值就降级。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from core import errors
from core.constants import CATEGORY_STALE_SECONDS
from core.utils import first_text, now_ts
from models.category import Category
from services.account_service import map_upstream_error
from services.cache import TTLCache, get_cache, ttl_from_env
from services.miaoshou import MiaoshouClient, MiaoshouError

GLOBAL_SCOPE = 0

# --- 缓存策略 ---------------------------------------------------------------
# 类目很少变，默认 300 秒；AOHUANG_CATEGORY_CACHE_TTL（秒）可覆盖。
CATEGORY_CACHE_TTL_ENV = "AOHUANG_CATEGORY_CACHE_TTL"
CATEGORY_CACHE_TTL_DEFAULT = 300.0
CATEGORY_CACHE_MAX_ENTRIES = 64

_CACHE: TTLCache | None = None


def _cache() -> TTLCache:
    global _CACHE
    if _CACHE is None:
        _CACHE = get_cache("categories", max_entries=CATEGORY_CACHE_MAX_ENTRIES,
                           default_ttl=CATEGORY_CACHE_TTL_DEFAULT)
    return _CACHE


def cache_ttl() -> float:
    return ttl_from_env(CATEGORY_CACHE_TTL_ENV, CATEGORY_CACHE_TTL_DEFAULT)


def cache_prefix(site: str, account_id: int | None = None) -> str:
    """按「站点 × 作用域」分前缀：同步成功后只失效这一棵树的缓存。"""
    scope = int(account_id) if account_id else GLOBAL_SCOPE
    return f"site:{(site or 'MY').upper()}:scope:{scope}:"


def invalidate(site: str = "", account_id: int | None = None) -> int:
    """失效某棵类目树的进程内缓存；``site`` 为空则清空全部类目缓存。"""
    if not site:
        return _cache().clear()
    return _cache().invalidate(cache_prefix(site, account_id))



def flatten_tree(tree: Any, *, site: str = "MY", parent_cid: str | None = None,
                 prefix: list[str] | None = None, depth: int = 0,
                 account_id: int = GLOBAL_SCOPE, stamp: float | None = None,
                 sort_counter: list[int] | None = None
                 ) -> list[dict[str, Any]]:
    """把妙手 cateTree（dict 或 list）压平成带 path / depth 的类目行。"""
    nodes: list[Any] = []
    if isinstance(tree, Mapping):
        nodes = list(tree.values())
    elif isinstance(tree, list):
        nodes = list(tree)
    rows: list[dict[str, Any]] = []
    if sort_counter is None:
        sort_counter = [0]
    current_sort = sort_counter[0]
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        cid = str(node.get("cid") or "").strip()
        if not cid:
            continue
        name = first_text(node.get("name"))
        name_cn = first_text(node.get("nameChinese"), node.get("nameCn"))
        label = name_cn or name or cid
        path = (prefix or []) + [label]
        children = node.get("children")
        child_rows = flatten_tree(children, site=site, parent_cid=cid, prefix=path,
                                 depth=depth + 1, account_id=account_id, stamp=stamp,
                                 sort_counter=sort_counter)
        is_leaf = not child_rows
        rows.append({
            "account_id": account_id,
            "cid": cid,
            "parent_cid": parent_cid,
            "site": site.upper(),
            "name": name,
            "name_cn": name_cn,
            "path": "/".join(path),
            "depth": depth,
            "is_leaf": bool(node.get("isLastLevel") == "1") or is_leaf,
            "sort": current_sort,
            # 平台标记的「不可选」。不收集的话，下拉里看不出这个类目被禁用，
            # 运营选完到发布时才被平台拒。
            "disabled": bool(node.get("disabled")) or str(node.get("disabled")) == "1",
            "aid": first_text(node.get("aid")) or None,
            "fid": first_text(node.get("fid")) or None,
            "synced_at": stamp or now_ts(),
        })
        rows.extend(child_rows)
        sort_counter[0] += 1
        current_sort = sort_counter[0]
    return rows


def sync_from_response(db: Session, response: Mapping[str, Any], *, site: str,
                       account_id: int | None = None) -> dict[str, Any]:
    """把一次 category_tree 的返回整棵替换入库（同 scope + site 先删后插）。"""
    data = (response or {}).get("data") or response or {}
    tree = data.get("cateTree") if isinstance(data, Mapping) else None
    if tree is None:
        tree = data.get("tree") or data.get("list")
    if tree in (None, {}, []):
        raise errors.upstream("妙手没有返回类目树", reason="cateTree_missing")
    scope = int(account_id) if account_id else GLOBAL_SCOPE
    site_code = (site or "MY").upper()
    stamp = now_ts()
    rows = flatten_tree(tree, site=site_code, account_id=scope, stamp=stamp)
    if not rows:
        raise errors.upstream("妙手返回的类目树为空", reason="cateTree_empty")
    # 入库前先体检，别等发布时才在「类目异常」上报错——那时报错位置离原因很远。
    # 这里查的是「入库后仍然坏」的那几类问题：重复 cid 会被唯一索引吞掉（叶子
    # 凭空消失，界面上少一批类目却没人知道少了什么）、父节点缺失会让面包屑断裂。
    #
    # **只拦致命的两类**：重复 cid 会让数据丢失，父节点缺失会让树挂不住。
    # 根节点问题（missingRoot / 双根）只记进报告不拦——sync 的口径是「整棵替换」，
    # 上游偶尔返回多棵根时，老版也能用，硬拦会让用户彻底同步不了类目，
    # 那比「树有点歪」严重得多。
    report = validate_rows(rows, root_cid=_guess_root_cid(rows))
    if report["duplicateCidTotal"] or report["parentErrorTotal"]:
        raise errors.upstream(
            "妙手返回的类目树有问题，已放弃本次同步（避免写坏本地类目缓存）",
            reason="cateTree_invalid",
            duplicateCidTotal=report["duplicateCidTotal"],
            duplicateCids=report["duplicateCids"][:5],
            parentErrorTotal=report["parentErrorTotal"],
            parentErrors=report["parentErrors"][:5],
        )
    db.query(Category).filter(Category.account_id == scope,
                              Category.site == site_code).delete(synchronize_session=False)
    db.bulk_insert_mappings(Category, rows)
    db.flush()
    leaves = sum(1 for row in rows if row["is_leaf"])
    return {
        "site": site_code,
        "accountId": str(scope) if scope else None,
        "total": len(rows),
        "leaf": leaves,
        "syncedAt": stamp,
        # 体检报告一起返回：树有点歪（比如多个根）时界面上能看见，不用猜
        "validate": report,
    }


def sync(db: Session, client: MiaoshouClient, *, site: str,
         account_id: int | None = None) -> dict[str, Any]:
    try:
        response = client.category_tree(site)
    except MiaoshouError as exc:
        raise map_upstream_error(exc) from None
    return sync_from_response(db, response, site=site, account_id=account_id)


def scope_rows(db: Session, site: str, account_id: int | None = None) -> tuple[list[Category], int]:
    """取该站点缓存：优先账号自己的，其次全局哨兵。"""
    site_code = (site or "MY").upper()
    scope = int(account_id) if account_id else GLOBAL_SCOPE
    rows = (db.query(Category)
            .filter(Category.account_id == scope, Category.site == site_code)
            .order_by(Category.sort.asc(), Category.id.asc()).all())
    if rows:
        return rows, scope
    rows = (db.query(Category)
            .filter(Category.account_id == GLOBAL_SCOPE, Category.site == site_code)
            .order_by(Category.sort.asc(), Category.id.asc()).all())
    return rows, GLOBAL_SCOPE if rows else scope


def build_tree(rows: list[Category], *, root_cid: str | None = None,
               depth: int = 2, leaf_only: bool = False) -> list[dict[str, Any]]:
    """按 parent_cid 组装树，最多展开 depth 层。"""
    by_parent: dict[str | None, list[Category]] = {}
    by_cid: dict[str, Category] = {}
    for row in rows:
        by_cid[row.cid] = row
        by_parent.setdefault(row.parent_cid or None, []).append(row)

    if root_cid:
        root = by_cid.get(str(root_cid))
        if root is None:
            raise errors.not_found(f"类目 {root_cid} 不在当前类目树里",
                                   rootCid=str(root_cid))
        start = [root]
        base_level = max(0, int(root.depth))
    else:
        start = by_parent.get(None, [])
        base_level = 0

    def node_of(row: Category, level: int) -> dict[str, Any]:
        children: list[dict[str, Any]] = []
        if level < depth:
            for child in by_parent.get(row.cid, []):
                if leaf_only and not child.is_leaf:
                    deeper = node_of(child, level + 1)
                    children.extend(deeper.get("children") or [])
                    continue
                children.append(node_of(child, level + 1))
        return {
            "cid": row.cid,
            "parentCid": row.parent_cid or None,
            "name": row.name,
            "nameZh": row.name_cn,
            "path": row.path,
            "depth": row.depth,
            "isLeaf": bool(row.is_leaf),
            "disabled": bool(row.disabled),
            "aid": row.aid or None,
            "fid": row.fid or None,
            "children": children,
        }

    result = [node_of(row, base_level) for row in start]
    if leaf_only:
        result = _flatten_leaves(result)
    return result


def _flatten_leaves(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    leaves: list[dict[str, Any]] = []
    for node in nodes:
        if node["isLeaf"] or not node["children"]:
            leaves.append({**node, "children": []})
        else:
            leaves.extend(_flatten_leaves(node["children"]))
    return leaves


def find_by_cid(db: Session, cid: str, *, site: str = "",
                account_id: int | None = None) -> Category | None:
    query = db.query(Category).filter(Category.cid == str(cid))
    if site:
        query = query.filter(Category.site == site.upper())
    if account_id:
        scoped = query.filter(Category.account_id == int(account_id)).first()
        if scoped is not None:
            return scoped
    return query.order_by(Category.account_id.asc().nullsfirst()).first()


def path_of(db: Session, cid: str, *, site: str = "",
            account_id: int | None = None) -> str:
    row = find_by_cid(db, cid, site=site, account_id=account_id)
    return row.path if row is not None else ""


def label_of(db: Session, cid: str, *, site: str = "",
             account_id: int | None = None) -> str:
    row = find_by_cid(db, cid, site=site, account_id=account_id)
    if row is None:
        return ""
    return row.name_cn or row.name or row.cid


def resolve(db: Session, cid: Any, *, site: str = "", account_id: int | None = None
            ) -> tuple[Category | None, bool]:
    """返回 (类目行, 是否 stale)。无缓存时 stale=True。"""
    text = str(cid or "").strip()
    if not text:
        return None, False
    row = find_by_cid(db, text, site=site, account_id=account_id)
    if row is None:
        return None, True
    stale = (now_ts() - float(row.synced_at or 0)) > CATEGORY_STALE_SECONDS
    return row, stale


def _guess_root_cid(rows: list[dict[str, Any]]) -> str:
    """猜根节点 cid，供 `validate_rows` 检查「根带了父节点」这一类问题。

    依赖倒置：`validate_rows` 要求调用方告诉它根是谁，但妙手的
    `cateTree` 返回的是一个列表，根并没有单独标出来。这里的口径是
    「最浅层、且有多行的那一层」——真正常见的形状是 2~3 个一级根
    并列，取第一个当参照点就够用（这个参数只用于校验，不影响入库）。
    猜错也不致命：猜到的不是真根时，`validate_rows` 只会少报一类问题，
    「重复 cid / 父节点缺失」这两个真正致命的判定与根本身无关。
    """
    depths = [int(row.get("depth") or 0) for row in rows]
    if not depths:
        return ""
    shallowest = min(depths)
    for row in rows:
        if int(row.get("depth") or 0) == shallowest:
            return str(row.get("cid") or "")
    return ""


def validate_rows(rows: list[dict[str, Any]], *, root_cid: str = ""
                  ) -> dict[str, Any]:
    """校验一棵待入库的类目树，返回体检报告（老版 validate_category_library）。

    四类问题都会让类目树在发布时出错，且报错位置离原因很远，所以入库前先查：
      · 重复 cid —— 唯一索引会吞掉后一条，叶子凭空消失；
      · 父节点不存在 —— 面包屑断裂，界面上该类目挂不到树里；
      · 根节点不存在 —— 整棵树没有入口；
      · 根节点的 parentCid 非空 —— 双根，遍历会绕圈。
    这里不抛错，把问题列出来让调用方决定（导入流程需要「先看再决定」）。
    """
    by_cid: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for row in rows:
        cid = str(row.get("cid") or "")
        if not cid:
            continue
        if cid in by_cid:
            duplicates.append(cid)
        by_cid[cid] = row
    root = str(root_cid or "")
    parent_errors: list[str] = []
    for cid, row in by_cid.items():
        parent = row.get("parent_cid")
        if cid == root:
            if parent not in (None, "", 0, "0"):
                parent_errors.append(f"根节点 {cid} 带了父节点 {parent}")
        elif parent in (None, "", 0, "0"):
            continue
        elif str(parent) not in by_cid:
            parent_errors.append(f"{cid} 的父节点 {parent} 不存在")
    missing_root = bool(root) and root not in by_cid
    return {
        "total": len(rows),
        "uniqueCid": len(by_cid),
        "leafTotal": sum(1 for row in rows if row.get("is_leaf")),
        "disabledTotal": sum(1 for row in rows if row.get("disabled")),
        "duplicateCids": duplicates[:20],
        "duplicateCidTotal": len(duplicates),
        "parentErrors": parent_errors[:20],
        "parentErrorTotal": len(parent_errors),
        "missingRoot": missing_root,
        "ok": not duplicates and not parent_errors and not missing_root,
    }


def counts(db: Session, site: str = "", account_id: int | None = None) -> dict[str, Any]:
    rows, scope = scope_rows(db, site or "MY", account_id)
    synced = max((row.synced_at or 0) for row in rows) if rows else None
    return {
        "total": len(rows),
        "leaf": sum(1 for row in rows if row.is_leaf),
        # 被平台禁用的类目数：界面可以直接提示「这棵树里有 N 个不可选」
        "disabledTotal": sum(1 for row in rows if row.disabled),
        "uniqueCid": len({row.cid for row in rows}),
        "cached": bool(rows),
        "scope": scope,
        "syncedAt": synced,
    }


# ===========================================================================
# 类目三档兜底 + 必填属性校验
# ---------------------------------------------------------------------------
# 老版（tk-product-automation）把「这件商品到底归哪个类目」拆成三段：
#   shoe_workflow.extract_category_id      :77-88   AI 匹配结果必须唯一
#   shoe_workflow.category_tree_index      :91-113  类目树 → 名字索引（只收末级）
#   shoe_workflow.fallback_category_id     :116-138 关键词兜底
#   product_workflow._resolve_category     :555-602 三段串起来
#   shoe_workflow.extract_category_requirements  :141-158 抠必填项
#   shoe_workflow.validate_category_requirements :160-180 校验必填项
#
# 新版原来一条都没有：采集箱商品没 cid 时只被 pre_save_check 规则 2 一句
# 「category cid is missing」挡住，运营根本不知道要去妙手手选类目，整批入不了仓。
# 这里按老版语义补齐，**不自己发明规则**。
# ===========================================================================

# 老版 fallback_category_id :119 的关键词表，一个词都没改。
# 这是「已验证过的末级类目」清单的入口条件：标题里没有这些词，就不许乱猜。
CATEGORY_KEYWORDS = ("拖鞋", "凉拖", "家居拖", "沙滩拖", "防滑拖鞋",
                     "slipper", "sandal", "selipar")

# 老版 :135 认定的两个末级类目名（中文名 + 马来语站点名）。
# 关键词命中只说明「像拖鞋」，落到哪个 cid 还得靠这两个名字在类目树里找。
CATEGORY_FALLBACK_NAMES = ("运动拖鞋", "sandal sukan")

# 三档全失败时的提示语。老版 product_workflow._resolve_category :595-598 的
# 原话是「请**到**妙手给这件商品手动选好类目后再处理」，这里按运维口径统一成
# 「请**在**妙手…」—— 两版差别只有一个介词，语义完全一致，而「请在妙手」
# 与界面上的操作按钮（在妙手页面选类目）措辞一致，运营不会以为是两个地方。
# 关键是后半句必须留着：没有 cid 时妙手不会展开颜色/尺码/重量，运营看到的
# 「商品没颜色没尺码」不是 bug，而是类目没选的连带结果。
MANUAL_CATEGORY_MESSAGE = (
    "该商品还没有选类目，妙手的 AI 也匹配不出来。"
    "请在妙手给这件商品手动选好类目后再处理；"
    "没选类目时颜色、尺码、重量都不会正常显示"
)

# 老版 extract_category_id :86-87 的原话。
NOT_UNIQUE_MESSAGE = "AI 类目匹配未返回唯一类目，请在妙手页面选择后再保存"


def _fold(value: Any) -> str:
    """名字归一：去空白 + casefold。老版每处都写一遍，这里收成一个函数。"""
    return str(value or "").strip().casefold()


def _iter_mappings(value: Any):
    """深度遍历响应里的所有 dict。

    老版 `_first_mapping`（shoe_workflow:54-62）就是这个形状：上游把 cid 放在
    第几层不固定，与其猜路径，不如把所有 dict 都翻一遍再按 key 找。
    """
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from _iter_mappings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_mappings(nested)


def extract_category_id(response: Mapping[str, Any]) -> str:
    """从 AI 类目匹配响应里取**唯一**的 cid（老版 shoe_workflow:77-88）。

    四个候选键是老版实测过的别名：cid / categoryId / category_id / targetCid。
    「唯一」是硬要求：上游有时把同一棵树上的多个候选都吐出来，取第一个会
    猜错类目，而错类目的必填项完全不同 —— 宁可报错让人去手选。
    """
    data = (response or {}).get("data", response) or {}
    found: list[str] = []
    for mapping in _iter_mappings(data):
        for key in ("cid", "categoryId", "category_id", "targetCid"):
            value = mapping.get(key)
            if value not in (None, "", 0, "0"):
                found.append(str(value))
    unique = list(dict.fromkeys(found))
    if len(unique) != 1:
        raise errors.AppError(
            errors.UNPROCESSABLE, NOT_UNIQUE_MESSAGE, status=422,
            details={"candidateCids": unique[:20], "candidateCount": len(unique)})
    return unique[0]


def name_index(db: Session, account_id: int | None = None, *,
               site: str = "") -> dict[str, list[str]]:
    """末级类目的「名字 → [cid]」索引（老版 category_tree_index :91-113）。

    三个条件照抄老版：
      · 只收末级（is_leaf）—— 非末级类目保存商品会被平台拒；
      · 跳过 disabled —— 平台标了不可选，收进来等于埋雷；
      · 名字同时收 name 与 name_cn，都 casefold + strip —— 类目树里中英文名
        并存，运营搜哪个都得能命中。

    与老版唯一的差别：老版遍历内存里的 cateTree，这里遍历已落库的 categories
    表。两者内容一致（入库就是 flatten_tree 压平的同一棵树），好处是命中
    不需要再回源，也不会因为上游抖动而退化成「找不到类目」。
    """
    query = db.query(Category)
    if site:
        query = query.filter(Category.site == site.upper())
    if account_id:
        query = query.filter(Category.account_id == int(account_id))
    index: dict[str, list[str]] = {}
    for row in query.all():
        if not row.is_leaf or row.disabled:
            continue
        cid = str(row.cid or "")
        if not cid:
            continue
        for label in (row.name_cn, row.name):
            folded = _fold(label)
            if folded:
                bucket = index.setdefault(folded, [])
                if cid not in bucket:
                    bucket.append(cid)
    return index


def fallback_by_keyword(db: Session, text: Any, *, account_id: int | None = None,
                        site: str = "") -> str:
    """关键词兜底：标题命中拖鞋词表 → 在本地类目树里找唯一末级 cid。

    老版 fallback_category_id（:116-138）的三道闸，顺序不能换：
      1. 标题里必须出现 CATEGORY_KEYWORDS 之一，否则直接拒绝 —— 这是
         「只对已确认过的鞋类生效」的保险丝，去掉它就会给任意商品瞎猜类目；
      2. 在索引里按 CATEGORY_FALLBACK_NAMES 取候选，多个名字的命中合并去重；
      3. 候选**必须恰好 1 个**，0 个或 2 个都抛错并说明原因。

    老版第 3 步的报错是「类目树中未找到唯一的运动拖鞋末级类目」，
    这里保留同样语义，但把「命中几个」也带进 details —— 运营看到「命中 2 个」
    才知道要先去类目树里清理重名，而不是反复重试。
    """
    title = _fold(text)
    if not any(_fold(word) in title for word in CATEGORY_KEYWORDS):
        raise errors.AppError(
            errors.UNPROCESSABLE,
            "AI 类目匹配失败，且未命中已确认的本地类目规则",
            status=422, details={"keywords": list(CATEGORY_KEYWORDS)})
    index = name_index(db, account_id, site=site)
    matches: list[str] = []
    for name in CATEGORY_FALLBACK_NAMES:
        for cid in index.get(_fold(name), []):
            if cid not in matches:
                matches.append(cid)
    if len(matches) != 1:
        raise errors.AppError(
            errors.UNPROCESSABLE,
            f"类目树中未找到唯一的运动拖鞋末级类目（命中 {len(matches)} 个）",
            status=422,
            details={"candidateCids": matches[:20], "candidateCount": len(matches),
                     "lookedFor": list(CATEGORY_FALLBACK_NAMES)})
    return matches[0]


def resolve_category(db: Session, *, client: Any, detail_id: Any, shop_id: Any,
                     info: Mapping[str, Any], account_id: int | None = None,
                     site: str = "") -> dict[str, Any]:
    """三档兜底：已有 cid → AI 匹配 → 本地库名字反查 → 关键词兜底。

    判定顺序（与老版 product_workflow._resolve_category :555-602 一致）：

    1. ``info`` 里已有 cid 且非空 → ``source="existing"``。
       老版还会拿本地类目库核对一次；新版没有「已验证类目库」这个文件，
       类目树就是库，所以这一步只做「非空即认」—— 空值才需要往下找。
    2. AI 匹配 ``match_category`` → ``extract_category_id`` 要求唯一。
       不唯一 / 上游报错 → 落到第 3 档，**不抛错**。
    3. 本地库名字反查：AI 给了候选但不唯一时，用标题去 name_index 里找；
       命中恰好 1 个就用它。
    4. 关键词兜底：标题命中拖鞋词表，且在类目树里找到唯一的「运动拖鞋」。
    5. 全失败 → **不抛错**，返回空 cid + 老版原始提示语。
       为什么不抛错：调用方（采集箱详情 / 路由）要把这句话原样显示给运营，
       抛异常会被上层改写成 500 或通用错误，提示语就丢了。

    参数 ``client`` 允许为 None（路由只查本地库时不必连上游），此时第 2 档跳过。
    """
    current = str((info or {}).get("cid") or "").strip()
    if current:
        row = find_by_cid(db, current, site=site, account_id=account_id)
        return {
            "cid": current,
            "source": "existing",
            "message": "",
            "name": (row.name if row is not None else "") or "",
            "nameChinese": (row.name_cn if row is not None else "") or "",
        }

    ai_error = ""
    ambiguous: list[str] = []
    ai_cid = ""
    if client is not None:
        try:
            ai_cid = extract_category_id(client.match_category(detail_id, [int(shop_id)]))
        except errors.AppError as exc:
            # 不唯一。候选只记进 reason 供排查，**绝不能取第一个用**——取第一个
            # 就是猜类目，而错类目的必填项完全不同（老版 :86-87 要求「必须唯一」
            # 正是这个原因）。这里清空候选，直接落下一档。
            ai_error = str(exc)
            ambiguous = [str(c) for c in
                         ((getattr(exc, "details", None) or {}).get("candidateCids") or [])]
        except Exception as exc:  # noqa: BLE001 - 上游任何失败都要能落下一档
            ai_error = str(exc)
        if ai_cid:
            row = find_by_cid(db, ai_cid, site=site, account_id=account_id)
            return {
                "cid": ai_cid,
                "source": "ai",
                "message": "",
                "name": (row.name if row is not None else "") or "",
                "nameChinese": (row.name_cn if row is not None else "") or "",
            }

    # 第 3 档：本地类目库名字反查。AI 不唯一时它的候选本身不可信，
    # 用商品标题去末级类目名里找一个精确命中的，命中唯一才采信。
    title = _fold((info or {}).get("title"))
    library_error = ""
    if title:
        index = name_index(db, account_id, site=site)
        hits: list[str] = []
        for name, cids in index.items():
            # 只拿至少 2 个字符的类目名去撞标题：类目树里有大量单字节点名
            # （「鞋」「男」），子串命中它们纯属噪音，会把「唯一命中」搅成
            # 「命中 N 个」，等于把这一档废掉。
            if len(name) >= 2 and name in title:
                for cid in cids:
                    if cid not in hits:
                        hits.append(cid)
        if len(hits) == 1:
            cid = hits[0]
            row = find_by_cid(db, cid, site=site, account_id=account_id)
            return {
                "cid": cid,
                "source": "library",
                "message": "",
                "name": (row.name if row is not None else "") or "",
                "nameChinese": (row.name_cn if row is not None else "") or "",
            }
        library_error = (f"本地类目库命中 {len(hits)} 个末级类目"
                         if hits else "本地类目库里没有与标题匹配的末级类目")

    # 第 4 档：关键词兜底。
    try:
        cid = fallback_by_keyword(db, title, account_id=account_id, site=site)
    except errors.AppError as exc:
        reason = " / ".join(item for item in (ai_error, library_error, str(exc)) if item)
        if ambiguous:
            # 把 AI 吐出来的多个候选写进 reason：运营据此能判断是「类目树里有
            # 重名」还是「AI 真没匹配上」，两种情况的处理动作不同。
            reason = f"AI 候选不唯一：{','.join(ambiguous[:10])}；{reason}"
        return {
            "cid": "",
            "source": "",
            "message": MANUAL_CATEGORY_MESSAGE,
            "reason": reason,
        }
    row = find_by_cid(db, cid, site=site, account_id=account_id)
    return {
        "cid": cid,
        "source": "keyword",
        "message": "",
        "name": (row.name if row is not None else "") or "",
        "nameChinese": (row.name_cn if row is not None else "") or "",
    }


# --- 类目必填属性 -----------------------------------------------------------
# 老版 shoe_workflow.extract_category_requirements :141-158 /
# validate_category_requirements :160-180。新版全库原本零命中
# （packageDimensionIsRequired / categoryMetadata），等于「类目要求包裹尺寸
# 或必填属性」这件事从来没被检查过：商品照样入仓，上架时才被平台拒。

# 上游把「必填」写在三个不同字段名上，三选一都算必填。
_MANDATORY_FLAGS = ("isMandatory", "required", "isRequired")


def _truthy(value: Any) -> bool:
    """上游布尔字段实测混着 "1"/"true"/1/True，统一判。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return _fold(value) in {"1", "true", "yes", "y", "t"}


def _attr_name(attr: Mapping[str, Any]) -> str:
    """属性显示名：老版 :174 的回退链，一个字段名都不猜新的。"""
    for key in ("name", "attrName", "attributeName", "attributeNameAlias", "attrId"):
        text = str(attr.get(key) or "").strip()
        if text:
            return text
    return ""


def extract_requirements(meta: Any) -> dict[str, Any]:
    """从类目元数据里抠出「发布这件商品必须满足什么」。

    返回 ``{"packageDimensionRequired": bool, "requiredAttributes": [名字…]}``。

    结构解析照抄老版 :146-155：
      · 先剥一层 ``data``；
      · 元数据体可能是 ``categoryMetadata`` / ``metadata`` / 就是 data 本身；
      · 必填项来自 ``categorySaleAttrList`` + ``categoryProductAttrList``
        （也认 ``saleAttributeList`` / ``productAttributeList`` 这两个别名）；
      · 任一条目带 isMandatory / required / isRequired 就算必填。

    与老版唯一的差别：老版结构异常时抛 TransformError，这里返回空要求。
    理由：这是「保存前校验」的输入，拿不到要求时应该退化成「不额外拦」，
    而不是让整次入仓因为上游多包了一层而失败。
    """
    data = (meta or {}).get("data", meta) if isinstance(meta, Mapping) else meta
    if not isinstance(data, Mapping):
        return {"packageDimensionRequired": False, "requiredAttributes": []}
    body = data.get("categoryMetadata") or data.get("metadata") or data
    if not isinstance(body, Mapping):
        return {"packageDimensionRequired": False, "requiredAttributes": []}
    config = body.get("categoryConfig") or {}
    if not isinstance(config, Mapping):
        config = {}
    # packageDimensionIsRequired 的落点不固定：实测上游把它放在 categoryConfig
    # 里（老版 :163 就是只读 config），但也有响应把它直接放在元数据体上。
    # 先看 categoryConfig（有该键就以它为准，False 也算明确答案），没有才回落
    # 到元数据体 —— 否则「类目要求包裹尺寸」这条会被静默漏判。
    if "packageDimensionIsRequired" in config:
        package_required = _truthy(config.get("packageDimensionIsRequired"))
    else:
        package_required = _truthy(body.get("packageDimensionIsRequired"))
    sale = body.get("categorySaleAttrList") or body.get("saleAttributeList") or []
    product = body.get("categoryProductAttrList") or body.get("productAttributeList") or []
    names: list[str] = []
    for attr in [*(sale if isinstance(sale, list) else []),
                 *(product if isinstance(product, list) else [])]:
        if not isinstance(attr, Mapping):
            continue
        if not any(_truthy(attr.get(flag)) for flag in _MANDATORY_FLAGS):
            continue
        label = _attr_name(attr)
        if label and label not in names:
            names.append(label)
    return {
        "packageDimensionRequired": package_required,
        "requiredAttributes": names,
    }


# 老版 :163-164 检查的三个字段，缺任一个都算「没填完整」。
_PACKAGE_KEYS = ("packageLength", "packageWidth", "packageHeight")


def _filled_names(payload: Mapping[str, Any]) -> set[str]:
    """商品上已经填了值的属性名集合（casefold）。

    老版 :166-171 收两处：
      · productAttributes / productAttributeList —— 普通商品属性；
      · skuPropertyList —— SKU 维度属性（颜色、尺码），运营常在 SKU 里填。
    漏掉后者会把「填在 SKU 里」误判成没填，所以两处都要收。
    """
    names: set[str] = set()
    for key in ("productAttributes", "productAttributeList"):
        for attr in (payload.get(key) or []):
            if isinstance(attr, Mapping):
                label = _attr_name(attr)
                if label:
                    names.add(_fold(label))
    for prop in (payload.get("skuPropertyList") or []):
        if isinstance(prop, Mapping):
            label = _attr_name(prop)
            if label:
                names.add(_fold(label))
    return names


def validate_requirements(info: Mapping[str, Any], requirements: Any) -> list[str]:
    """类目必填项校验；返回问题清单，空表示通过。

    两条检查与文案照抄老版 validate_category_requirements :163-178：
      · ``packageDimensionIsRequired`` 且长/宽/高缺任一 →
        「类目要求包裹长宽高，当前商品未填完整」；
      · 必填属性名在商品上找不到 →
        「类目必填属性未填：材质、产地」（顿号分隔，去重保序）。

    ``requirements`` 允许是 ``extract_requirements`` 的返回值，也允许直接传
    原始上游元数据（调用方少写一次转换）。
    """
    req = requirements
    if not isinstance(req, Mapping) or "requiredAttributes" not in req:
        req = extract_requirements(req)
    issues: list[str] = []
    payload = info or {}
    if (req.get("packageDimensionRequired")
            and any(_ci_value(payload, key) in (None, "") for key in _PACKAGE_KEYS)):
        issues.append("类目要求包裹长宽高，当前商品未填完整")
    filled = _filled_names(payload)
    missing: list[str] = []
    for label in (req.get("requiredAttributes") or []):
        text = str(label or "").strip()
        if text and _fold(text) not in filled and text not in missing:
            missing.append(text)
    if missing:
        issues.append("类目必填属性未填：" + "、".join(missing))
    return issues


def _ci_value(payload: Mapping[str, Any], key: str) -> Any:
    """payload 字段大小写不统一（packageLength / packagelength），按不区分大小写取。"""
    if key in payload:
        return payload.get(key)
    lowered = key.casefold()
    for name, value in payload.items():
        if str(name).casefold() == lowered:
            return value
    return None
