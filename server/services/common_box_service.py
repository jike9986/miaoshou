"""公共采集箱投放服务：把仓库成品推送到妙手公共采集箱。

与「认领到店铺采集箱」的区别：
  · 认领到采集箱 = claim_common_to_platform（从公共箱复制 → 覆盖成商品），
    目标是某个店铺的采集箱，为发布做准备；
  · 推送到公共箱 = add_common_collect_box_detail，把成品放进公共货源池，
    供其他学员在妙手后台「认领到平台采集箱」。

公共箱接口的关键要求（实测踩坑记录，见 push_common.py）：
  1. title 必填；
  2. skuPropertyList 的 attrId 不能为空（颜色 100000 / 尺码 100007）；
  3. skuMap 的 key 用规格名称拼接（;White;35;），不用 id；
  4. 必须传 colorMap / sizeMap，否则妙手会自动改名导致匹配失败。
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from services.miaoshou import MiaoshouClient, MiaoshouError

logger = logging.getLogger("aohuang.common_box")

# 类目销售规格 attrId（妙手固定值，颜色/尺码）
ATTR_ID_COLOR = "100000"
ATTR_ID_SIZE = "100007"


def build_common_body(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把仓库成品 payload 转成公共箱要求的 body。"""
    src = dict(payload or {})
    keep = (
        "title", "oriTitle", "cid", "site", "imgUrls", "notes", "weight",
        "packageLength", "packageWidth", "packageHeight",
        "isCodOpen", "deliveryOptionSetType", "sizeChart", "sizeChartType",
        "brandId", "productAttributes", "skuPropertyList", "skuMap",
        "manufacturerIds", "productCertifications", "responsiblePersonIds",
        "editModel",
    )
    cleaned = {key: src[key] for key in keep if key in src}
    cleaned["site"] = str(cleaned.get("site") or "MY")

    # 中文字段上游会拒
    for junk in ("brandName", "categoryName", "shopId"):
        cleaned.pop(junk, None)

    # --- skuPropertyList：attrId 必须非空 ---
    props: list[dict[str, Any]] = []
    color_prop: dict[str, Any] | None = None
    size_prop: dict[str, Any] | None = None
    for prop in (src.get("skuPropertyList") or []):
        if not isinstance(prop, Mapping):
            continue
        name = str(prop.get("attrName") or prop.get("propName") or "").strip()
        lower = name.lower()
        is_size = ("saiz" in lower or "尺码" in name or "尺寸" in name)
        attr_id = str(prop.get("attrId") or "").strip() or (
            ATTR_ID_SIZE if is_size else ATTR_ID_COLOR)
        entry: dict[str, Any] = {
            "attrId": attr_id,
            "attrName": name or ("Saiz" if is_size else "Warna"),
            "attrValueList": [],
        }
        for av in (prop.get("attrValueList") or []):
            if not isinstance(av, Mapping):
                continue
            label = str(av.get("attrValue") or "").strip()
            if not label:
                continue
            img = str(av.get("imgUrl") or av.get("imageUrl")
                      or (av.get("imgUrls") or [None])[0] or "")
            entry["attrValueList"].append({
                "attrValueId": str(av.get("attrValueId") or "").strip(),
                "attrValue": label,
                "imgUrl": img,
            })
        if not entry["attrValueList"]:
            continue
        if is_size:
            size_prop = entry
        else:
            color_prop = entry
        props.append(entry)
    cleaned["skuPropertyList"] = props

    # --- skuMap：key 用规格名称（按维度顺序），价格兜底 ---
    id_to_name: dict[str, str] = {}
    dim_ids: list[set[str]] = []
    for prop in props:
        ids: set[str] = set()
        for av in prop["attrValueList"]:
            vid = str(av.get("attrValueId") or "")
            label = str(av.get("attrValue") or "")
            if vid:
                ids.add(vid)
            if vid and label:
                id_to_name[vid] = label
        dim_ids.append(ids)

    old_map = src.get("skuMap") or {}
    new_map: dict[str, Any] = {}
    if isinstance(old_map, Mapping):
        for key, sku in old_map.items():
            if not isinstance(sku, Mapping):
                continue
            parts = [x.strip() for x in str(key).split(";") if x.strip()]
            ordered: list[str] = []
            leftover = list(parts)
            for ids in dim_ids:
                found = None
                for part in leftover:
                    if part in ids:
                        found = part
                        break
                if found is not None:
                    ordered.append(found)
                    leftover.remove(found)
            ordered.extend(leftover)
            names = [id_to_name.get(p, p) for p in ordered]
            entry = dict(sku)
            # 币种规则（负责人 2026-11-05 明确）：
            #   originPrice = 1688 人民币采购价，保持原值不要动；
            #   price / priceIncludeVat = 马币售价（仓库已按 成本÷1.633+2 算好）。
            # 之前把 originPrice 也写成马币，妙手按它算成本线就把
            # 价格全打到最低了。
            if not entry.get("price"):
                entry["price"] = entry.get("priceIncludeVat") or "0"
            if not entry.get("priceIncludeVat"):
                entry["priceIncludeVat"] = str(entry.get("price") or "0")
            entry.pop("shopIdToWarehouseIdAndStockMap", None)
            new_map[";" + ";".join(names) + ";"] = entry
    cleaned["skuMap"] = new_map

    # 顶层价格兜底：price/priceIncludeVat 用最低马币售价当参考价，
    # originPrice 保持人民币原值（从第一条 SKU 取），绝不写马币。
    active_skus = [v for v in new_map.values()
                   if isinstance(v, Mapping) and str(v.get("isDelete", "0")) != "1"]
    if active_skus:
        myr_prices = []
        cny_origin = None
        for sku in active_skus:
            try:
                value = float(sku.get("price") or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                myr_prices.append(value)
            if cny_origin is None:
                try:
                    origin_value = float(sku.get("originPrice") or 0)
                except (TypeError, ValueError):
                    origin_value = 0
                if origin_value > 0:
                    cny_origin = origin_value
        if myr_prices:
            cleaned.setdefault("price", str(min(myr_prices)))
            cleaned.setdefault("priceIncludeVat", str(min(myr_prices)))
        if cny_origin is not None:
            cleaned["originPrice"] = str(cny_origin)

    # --- colorMap / sizeMap：显式声明，防止妙手自动改名 ---

    def build_axis(prop: Mapping[str, Any] | None) -> dict[str, Any]:
        axis: dict[str, Any] = {}
        if not isinstance(prop, Mapping):
            return axis
        for av in prop.get("attrValueList") or []:
            label = str(av.get("attrValue") or "").strip()
            if not label:
                continue
            img = str(av.get("imgUrl") or "")
            axis[label] = {"name": label, "imgUrl": img,
                           "imgUrls": [img] if img else []}
        return axis

    if color_prop is not None:
        cleaned["colorMap"] = build_axis(color_prop)
        cleaned["colorPropName"] = str(color_prop.get("attrName") or "Warna")
    if size_prop is not None:
        cleaned["sizeMap"] = build_axis(size_prop)
        cleaned["sizePropName"] = str(size_prop.get("attrName") or "Saiz")

    return cleaned


def push_products_to_common_box(client: MiaoshouClient,
                                payloads: list[Mapping[str, Any]],
                                *,
                                progress=None) -> dict[str, Any]:
    """批量把成品推送到公共采集箱。

    返回 {ok, fail, results: [{productId?, detailId, title, status, reason}]}。
    """
    ok = 0
    fail = 0
    results: list[dict[str, Any]] = []
    for index, payload in enumerate(payloads, 1):
        body = build_common_body(payload)
        title = str(payload.get("title") or "")[:40]
        try:
            resp = client.add_common_box_detail(body)
            data = resp.get("data") or {}
            detail_id = (data.get("commonCollectBoxDetailId")
                         or data.get("detailId") or "")
            ok += 1
            results.append({"index": index, "detailId": str(detail_id),
                            "title": title, "status": "ok"})
        except MiaoshouError as exc:
            fail += 1
            results.append({"index": index, "title": title,
                            "status": "fail", "reason": str(exc)[:120]})
        except Exception as exc:  # noqa: BLE001
            fail += 1
            results.append({"index": index, "title": title,
                            "status": "fail",
                            "reason": "%s: %s" % (type(exc).__name__, str(exc)[:100])})
        if progress is not None:
            try:
                progress({"index": index, "total": len(payloads),
                          "ok": ok, "fail": fail,
                          "title": title})
            except Exception:  # noqa: BLE001
                pass
    return {"ok": ok, "fail": fail, "results": results}
