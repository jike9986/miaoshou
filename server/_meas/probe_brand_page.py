"""品牌分页缺口的本地验证（TestClient + FakeClient，不联网、不部署）。

背景：上游 `get_brand_list` 实测回 101 行（100 个品牌 + 1 行 brandId=0 的合计行）、
`totalCount=10000`、带 `nextPageToken`。新版 `GET /shops/brands` 原来只拿第一页，
第 101 个之后的品牌用户搜不到 —— 这是数据丢失。

本脚本逐条验任务书要求的 7 项：

  1. brand_list_page 正常返回一页，nextPageToken 透传
  2. brand_list_all 翻 3 页：第 1 页给 token、第 2 页给 token、第 3 页 token 为空
     → 停，pages=3
  3. brand_list_all 上游 token 永远不为空 → 撞 max_pages 停，truncated=True，
     不死循环（计数器断言调用次数 == max_pages）
  4. brand_list_all 翻页中途某页抛异常 → 已拿到的页保住，truncated=True + error
  5. 去重：两页返回同一个 brandId → 结果里只出现一次
  6. 缓存 key：pageToken 不同 → 缓存 key 不同（两次调用都打到上游）
  7. all=true 与 pageToken 同时给 → 以 all 为准

外加：`import main` 后路由表里有 GET /shops/brands，参数含 pageToken/all/name。

数据目录用临时目录（AOHUANG_DATA_DIR），绝不碰真实 app.db。
结果写 JSON 文件再读 —— 中文在 Windows 控制台会被吃成乱码。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="brandpage-"))
os.environ["AOHUANG_DATA_DIR"] = str(_TMP_DIR)
os.environ["TK_AUTOMATION_DATA_DIR"] = str(_TMP_DIR)
OUT = ROOT / "_meas" / "probe_brand_page.json"

from fastapi.testclient import TestClient  # noqa: E402

from api import API_PREFIX, reference  # noqa: E402
from core.config import get_settings  # noqa: E402
from db.session import Base, SessionLocal, engine  # noqa: E402
from main import app  # noqa: E402
from models.account import Account  # noqa: E402
from services.miaoshou import MiaoshouClient, MiaoshouError  # noqa: E402

results: list[dict] = []


def check(label: str, ok: bool, detail: object = "") -> None:
    results.append({"label": label, "ok": bool(ok), "detail": detail})


# --- 上游响应构造 -----------------------------------------------------------
def brand_payload(rows: list[dict], token: str = "", total: int | None = 10000) -> dict:
    """照抄 _meas/ref_upstream.json 里的真实信封形状（result/code/data.brandList）。"""
    data: dict = {"brandList": rows}
    if token:
        data["nextPageToken"] = token
    if total is not None:
        data["totalCount"] = total
    return {"result": "success", "code": "success", "message": "", "data": data}


def brand_row(brand_id: str, name: str = "", status: str = "UNAUTHORIEZD") -> dict:
    return {"brandId": brand_id, "brandName": name or f"brand-{brand_id}",
            "authorizedStatus": status}


class PageClient:
    """真 MiaoshouClient，只把最底层的 `post` 换成假的。

    为什么不用「重写 brand_list_page 的同形假对象」：那样保险丝、去重、异常
    保住已拿到的页这些逻辑全被测假实现绕过去了，等于没验。这里保留真类，
    连 body 怎么拼（pageToken 放 body 还是 header、空 token 传不传）都一起验。
    """

    def __init__(self, pages: list, *, fail_on: int | None = None) -> None:
        self.pages = pages            # 每项：dict（响应）或 callable(token)->dict
        self.fail_on = fail_on        # 第几次上游调用（1 起）抛异常
        self.calls: list[dict] = []   # 每次上游调用的 body
        # 绕开 __init__：那里会建 httpx.Client 并读系统代理，本地验证不需要联网
        self.client = object.__new__(MiaoshouClient)
        self.client.post = self._post  # type: ignore[method-assign]
        self.client.close = lambda: None  # type: ignore[method-assign]

    def _post(self, path: str, body=None) -> dict:
        body = dict(body or {})
        self.calls.append(body)
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise MiaoshouError("模拟上游中途失败", code="500", kind="error")
        index = len(self.calls) - 1
        if index >= len(self.pages):
            # 超出预置页数：当成「永远还有下一页」，专门喂给保险丝测试
            page = self.pages[-1]
        else:
            page = self.pages[index]
        token = str(body.get("pageToken") or "")
        return page(token) if callable(page) else page

    def close(self):
        return None

    @property
    def tokens(self) -> list[str]:
        return [str(b.get("pageToken") or "") for b in self.calls]


def call_all(client: PageClient, *, max_pages: int = 20, name: str = "") -> dict:
    return client.client.brand_list_all(18077147, max_pages=max_pages, name=name)


def call_page(client: PageClient, *, page_token: str = "", name: str = "") -> dict:
    return client.client.brand_list_page(18077147, page_token=page_token, name=name)


# --- 1. 单页 + token 透传 ---------------------------------------------------
def test_page() -> None:
    fake = PageClient([brand_payload([brand_row("1"), brand_row("2")],
                                     token="cGFnZV9udW1iZXI9Mg==")])
    page = call_page(fake)
    check("brand_list_page 返回一页 items", len(page["items"]) == 2, page.get("items"))
    check("brand_list_page 透传 nextPageToken",
          page["nextPageToken"] == "cGFnZV9udW1iZXI9Mg==", page.get("nextPageToken"))
    check("brand_list_page 透传 totalCount", page["totalCount"] == 10000,
          page.get("totalCount"))
    # nextPageToken 走 body，不是 header：上游路径 get_brand_list 只有 body 入参
    check("第一页 body 不带 pageToken（空游标不传）",
          "pageToken" not in fake.calls[0], fake.calls[0])
    check("body 里 shopId 是 int", fake.calls[0].get("shopId") == 18077147,
          fake.calls[0])
    check("默认 pageSize 不传（与老版 body 一致）",
          "pageSize" not in fake.calls[0], fake.calls[0])

    fake = PageClient([brand_payload([brand_row("1")], token="tok-2")])
    call_page(fake, page_token="tok-2", name="sony")
    check("pageToken 放在 body", fake.calls[0].get("pageToken") == "tok-2", fake.calls[0])
    check("name 放在 body", fake.calls[0].get("name") == "sony", fake.calls[0])

    # 空 token 页：上游不返回 nextPageToken 字段时必须是空串，不能是 None
    fake = PageClient([brand_payload([brand_row("3")], token="")])
    page = call_page(fake)
    check("末页 nextPageToken 为空串", page["nextPageToken"] == "", page.get("nextPageToken"))


# --- 2. 翻 3 页正常收敛 -----------------------------------------------------
def test_three_pages() -> None:
    fake = PageClient([
        brand_payload([brand_row("1"), brand_row("2")], token="tok-2"),
        brand_payload([brand_row("3"), brand_row("4")], token="tok-3"),
        brand_payload([brand_row("5")], token=""),
    ])
    result = call_all(fake)
    check("翻 3 页后停（token 空）", result["pages"] == 3, result["pages"])
    check("3 页共 5 条", len(result["items"]) == 5, len(result["items"]))
    check("3 页未截断", result["truncated"] is False, result["truncated"])
    check("3 页无 error", result["error"] == "", result["error"])
    check("翻页把上一页 token 传回下一次请求",
          fake.tokens == ["", "tok-2", "tok-3"], fake.tokens)


# --- 3. token 永不收敛 → 保险丝 --------------------------------------------
def test_fuse() -> None:
    counter = {"n": 0}

    def never_ending(token: str) -> dict:
        counter["n"] += 1
        return brand_payload([brand_row(f"id-{counter['n']}")],
                             token=f"tok-{counter['n'] + 1}")

    fake = PageClient([never_ending])
    result = call_all(fake, max_pages=5)
    check("token 永不空 → 撞 max_pages 停", result["pages"] == 5, result["pages"])
    check("token 永不空 → truncated=True", result["truncated"] is True,
          result["truncated"])
    check("不死循环：上游调用次数 == max_pages", len(fake.calls) == 5, len(fake.calls))
    check("保险丝触发时仍有 nextPageToken 供续翻",
          bool(result["nextPageToken"]), result["nextPageToken"])

    # max_pages=1 的极端值：只允许一次上游调用
    fake = PageClient([never_ending])
    result = call_all(fake, max_pages=1)
    check("max_pages=1 → 只调一次上游", len(fake.calls) == 1, len(fake.calls))
    check("max_pages=1 → truncated=True", result["truncated"] is True, result["truncated"])

    # max_pages<1 是调用方 bug，必须报错而不是静默返回空
    try:
        call_all(PageClient([never_ending]), max_pages=0)
        check("max_pages=0 → 抛错", False, "没有抛异常")
    except MiaoshouError as exc:
        check("max_pages=0 → 抛错", True, str(exc))


# --- 4. 中途失败保住已拿到的页 ---------------------------------------------
def test_midway_failure() -> None:
    fake = PageClient([
        brand_payload([brand_row("1"), brand_row("2")], token="tok-2"),
        brand_payload([brand_row("3")], token="tok-3"),
        brand_payload([brand_row("4")], token=""),
    ], fail_on=3)
    result = call_all(fake)
    check("中途失败：保住前两页（3 条）", len(result["items"]) == 3,
          [r["brandId"] for r in result["items"]])
    check("中途失败：pages 记已成功页数", result["pages"] == 2, result["pages"])
    check("中途失败：truncated=True", result["truncated"] is True, result["truncated"])
    check("中途失败：带 error 说明", "模拟上游中途失败" in str(result["error"]),
          result["error"])

    # 第一页就失败：一条都没有，不能假装成功
    fake = PageClient([brand_payload([brand_row("1")], token="")], fail_on=1)
    try:
        call_all(fake)
        check("首页失败 → 上抛（不返回空列表）", False, "没有抛异常")
    except MiaoshouError as exc:
        check("首页失败 → 上抛（不返回空列表）", True, str(exc))


# --- 5. 去重 ---------------------------------------------------------------
def test_dedup() -> None:
    fake = PageClient([
        brand_payload([brand_row("1"), brand_row("2")], token="tok-2"),
        # 上游相邻页重叠：brandId=2 又出现一次
        brand_payload([brand_row("2"), brand_row("3")], token=""),
    ])
    result = call_all(fake)
    ids = [row["brandId"] for row in result["items"]]
    check("两页重复 brandId 只出现一次", ids == ["1", "2", "3"], ids)
    check("去重后条数 3", len(ids) == 3, len(ids))


# --- 6. 缓存 key 必须带 pageToken / all / name ------------------------------
def test_cache_key() -> None:
    first = reference.brand_cache_key(18077147, page_token="")
    second = reference.brand_cache_key(18077147, page_token="tok-2")
    check("pageToken 不同 → 缓存 key 不同", first != second, [first, second])
    check("all=true 与单页 key 不同",
          reference.brand_cache_key(18077147, all_pages=True) != first,
          reference.brand_cache_key(18077147, all_pages=True))
    check("name 不同 → 缓存 key 不同",
          reference.brand_cache_key(18077147, name="abc")
          != reference.brand_cache_key(18077147, name="xyz"),
          reference.brand_cache_key(18077147, name="abc"))


def test_cache_end_to_end(client: TestClient, headers: dict) -> None:
    """真路由 + 假上游：两页必须都打到上游，第二页不能命中第一页的缓存。

    RouteClient 仍然是真 MiaoshouClient，只把 `post` 换掉 —— 路由调的是
    brand_list_page（它做信封拆解），假实现必须从 `post` 这一层喂上游原始信封，
    否则测的是假实现自己的返回结构，验不到真代码。
    """
    class RouteClient:
        def __init__(self) -> None:
            self.calls: list[str] = []
            # 造一个真 MiaoshouClient 实例（绕开 __init__，不建 httpx.Client），
            # 只替换 post；close 也一起换掉，否则会去摸不存在的 _client。
            self.client = object.__new__(MiaoshouClient)
            self.client.post = self._post      # type: ignore[method-assign]
            self.client.close = self.close     # type: ignore[method-assign]

        def _post(self, path: str, body=None) -> dict:
            body = dict(body or {})
            token = str(body.get("pageToken") or "")
            self.calls.append(token)
            if token:
                return brand_payload([brand_row("3")], token="")
            return brand_payload([brand_row("1"), brand_row("2")], token="tok-2")

        def close(self):
            return None

    fake = RouteClient()
    _install(fake.client)
    reference._cache().clear()

    resp = client.get(f"{API_PREFIX}/shops/brands?shopId=18077147", headers=headers)
    check("第一页 200", resp.status_code == 200,
          {"status": resp.status_code, "body": str(resp.text)[:400]})
    first = resp.json()
    check("第一页 cached=false", first.get("cached") is False, first.get("cached"))
    check("第一页带 nextPageToken", first.get("nextPageToken") == "tok-2",
          first.get("nextPageToken"))
    check("第一页 totalCount=10000", first.get("totalCount") == 10000,
          first.get("totalCount"))
    check("第一页 truncated=True（还有下一页）", first.get("truncated") is True,
          first.get("truncated"))
    check("第一页 brands 归一（brandId/authorized）",
          [b["brandId"] for b in first.get("brands") or []] == ["1", "2"],
          first.get("brands"))
    check("第一页 paging 向后兼容",
          (first.get("paging") or {}).get("returned") == 2
          and (first.get("paging") or {}).get("nextPageToken") == "tok-2",
          first.get("paging"))

    second = client.get(f"{API_PREFIX}/shops/brands?shopId=18077147&pageToken=tok-2",
                        headers=headers).json()
    check("第二页 cached=false（没命中第一页缓存）",
          second.get("cached") is False, second.get("cached"))
    check("两次请求都打到上游", len(fake.calls) == 2, fake.calls)
    check("上游收到的第二个 token 是 tok-2", fake.calls == ["", "tok-2"], fake.calls)
    check("第二页返回的是新数据", [b["brandId"] for b in second.get("brands") or []]
          == ["3"], second.get("brands"))
    check("第二页 nextPageToken 为 None（翻完了）",
          second.get("nextPageToken") is None, second.get("nextPageToken"))
    check("第二页 truncated=False", second.get("truncated") is False,
          second.get("truncated"))

    # 同一页重复请求 → 命中缓存（证明 key 稳定，不会每次都回源）
    third = client.get(f"{API_PREFIX}/shops/brands?shopId=18077147&pageToken=tok-2",
                       headers=headers).json()
    check("同页重复请求命中缓存", third.get("cached") is True, third.get("cached"))
    check("同页重复请求不再回源", len(fake.calls) == 2, fake.calls)

    # name 参与 key：搜 abc 不能命中不带 name 的缓存
    named = client.get(f"{API_PREFIX}/shops/brands?shopId=18077147&name=abc",
                       headers=headers).json()
    check("带 name 的请求没命中无 name 缓存", named.get("cached") is False,
          named.get("cached"))
    check("带 name 时多打一次上游", len(fake.calls) == 3, fake.calls)


# --- 7. all=true 优先 -------------------------------------------------------
def test_all_priority(client: TestClient, headers: dict) -> None:
    class AllClient:
        def __init__(self) -> None:
            self.all_calls: list[tuple] = []
            self.page_calls: list[str] = []

        def brand_list_page(self, shop_id, *, page_token="", page_size=100, name=""):
            self.page_calls.append(page_token)
            return {"items": [brand_row("p1")], "nextPageToken": "",
                    "totalCount": 1, "raw": brand_payload([brand_row("p1")])}

        def brand_list_all(self, shop_id, *, max_pages=20, name=""):
            self.all_calls.append((max_pages, name))
            return {"items": [brand_row("1"), brand_row("2")], "pages": 1,
                    "truncated": False, "totalCount": 2, "nextPageToken": "",
                    "error": ""}

        def close(self):
            return None

    fake = AllClient()
    _install(fake)
    reference._cache().clear()

    body = client.get(f"{API_PREFIX}/shops/brands?shopId=18077147"
                      "&all=true&pageToken=stale-token", headers=headers).json()
    check("all=true 与 pageToken 同给 → 走 brand_list_all",
          len(fake.all_calls) == 1 and not fake.page_calls,
          {"all": fake.all_calls, "page": fake.page_calls})
    check("all=true 时被忽略的游标回显", body.get("ignoredPageToken") is True,
          body.get("ignoredPageToken"))
    check("all=true 时 pageToken 回显为 None", body.get("pageToken") is None,
          body.get("pageToken"))
    check("all=true 返回全部 brands", len(body.get("brands") or []) == 2,
          body.get("brands"))
    check("all=true 时 pages=1", body.get("pages") == 1, body.get("pages"))
    check("all=true 且未截断 → complete=True", body.get("complete") is True,
          body.get("complete"))

    # maxPages 参数要透传成保险丝
    reference._cache().clear()
    client.get(f"{API_PREFIX}/shops/brands?shopId=18077147&all=true&maxPages=3",
               headers=headers)
    check("maxPages 透传为保险丝", fake.all_calls[-1][0] == 3, fake.all_calls[-1])

    # name 透传给 brand_list_all
    reference._cache().clear()
    client.get(f"{API_PREFIX}/shops/brands?shopId=18077147&all=true&name=sony",
               headers=headers)
    check("name 透传给 brand_list_all", fake.all_calls[-1][1] == "sony",
          fake.all_calls[-1])

    # 非法 maxPages → 400
    resp = client.get(f"{API_PREFIX}/shops/brands?shopId=18077147&all=true&maxPages=0",
                      headers=headers)
    check("maxPages=0 → 400", resp.status_code == 400, resp.status_code)

    # all 参数非法值 → 400
    resp = client.get(f"{API_PREFIX}/shops/brands?shopId=18077147&all=maybe",
                      headers=headers)
    check("all=maybe → 400", resp.status_code == 400, resp.status_code)


# --- 8. 路由表参数 ---------------------------------------------------------
def test_route_params() -> None:
    route = next((r for r in app.routes
                  if getattr(r, "path", "") == f"{API_PREFIX}/shops/brands"), None)
    check("路由表有 GET /shops/brands", route is not None, "")
    if route is None:
        return
    check("GET /shops/brands 是 GET", "GET" in (route.methods or set()),
          sorted(route.methods or []))
    names = {p.name for p in route.dependant.query_params}
    aliases = {p.alias for p in route.dependant.query_params}
    for wanted in ("pageToken", "all", "name", "maxPages", "shopId", "refresh"):
        # `all` 是 Python 关键字，入参只能叫 allPages，靠 alias 暴露成 all；
        # 所以对外可见的查询名要在 alias 集合里找，内部名集合里找不到 all。
        ok = wanted in aliases or wanted in names
        check(f"参数含 {wanted}", ok, {"names": sorted(names), "aliases": sorted(aliases)})
    # all 的对外名字必须是 all，不是 allPages（内部变量名换了，URL 不能变）
    by_name = {p.name: p for p in route.dependant.query_params}
    check("all 的别名就是 all", getattr(by_name.get("allPages"), "alias", None) == "all",
          getattr(by_name.get("allPages"), "alias", None))


# --- 装配 -------------------------------------------------------------------
_CURRENT: dict = {"client": None}


def _install(fake) -> None:
    """api/shops.py 是 `from services.account_service import account_client`，
    所以在**它自己的命名空间**里替换。"""
    from api import shops

    _CURRENT["client"] = fake
    shops.account_client = lambda _account, _fake=fake: _fake


def seed_account() -> None:
    db = SessionLocal()
    try:
        db.add(Account(name="brand-probe", app_key="probe-key",
                       app_secret_enc="probe-secret", site="MY",
                       is_default=True, status="active"))
        db.commit()
    finally:
        db.close()


def main() -> int:
    Base.metadata.create_all(bind=engine)

    # 纯客户端层（不需要 app / 数据库）
    test_page()
    test_three_pages()
    test_fuse()
    test_midway_failure()
    test_dedup()
    test_cache_key()
    test_route_params()

    token = get_settings().local_secret()
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        seed_account()
        test_cache_end_to_end(client, headers)
        test_all_priority(client, headers)

    failed = [item for item in results if not item["ok"]]
    payload = {"total": len(results), "passed": len(results) - len(failed),
               "failed": len(failed), "results": results}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("TOTAL %d PASS %d FAIL %d" % (len(results), len(results) - len(failed),
                                        len(failed)))
    for item in failed:
        print("FAIL", item["label"], "->", str(item["detail"])[:200])
    print("OUT " + str(OUT))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
