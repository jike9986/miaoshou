"""P1 双缺陷修复的本地验证（不联网、不部署、不扣费）。

覆盖两件事：
  1. 语言白名单：SITE_LANGUAGES 的每个值都必须在 SUPPORTED_LANGUAGES 里，
     且 check_language 改成「站点已知以站点为准，白名单只在站点未知时兜底」；
  2. 图片密钥验证入口：GET /images/verify 只查配置、POST /images/verify 要
     confirm 且真的发请求（这里用 mock 替掉上游，绝不产生费用）。

数据目录用临时目录（AOHUANG_DATA_DIR + TK_AUTOMATION_DATA_DIR），绝不碰真实
app.db。结果写 JSON 文件再读——中文在 Windows 控制台会被吃成乱码。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from itertools import chain
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="langverify-probe-"))
os.environ["AOHUANG_DATA_DIR"] = str(_TMP_DIR)
os.environ["TK_AUTOMATION_DATA_DIR"] = str(_TMP_DIR)
# 关键：本机真实存在老版遗留密钥（LOCALAPPDATA/tk-product-automation/*.txt），
# 不隔离的话「未配置」这条分支永远测不到——image_api_key() 会读到真密钥。
# 把 LOCALAPPDATA 指向空目录，模拟一台全新安装、什么都没配的机器。
_FAKE_LOCALAPPDATA = Path(tempfile.mkdtemp(prefix="langverify-localappdata-"))
os.environ["LOCALAPPDATA"] = str(_FAKE_LOCALAPPDATA)
os.environ.pop("AOHUANG_IMAGE_KEY", None)
OUT = ROOT / "_meas" / "probe_lang_verify.json"

from fastapi.testclient import TestClient  # noqa: E402

from api import API_PREFIX  # noqa: E402
from api.images import _smoke_png as _smoke_png_for_probe  # noqa: E402
from core import errors  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.constants import (  # noqa: E402
    LANGUAGE_LABELS,
    SITE_LANGUAGES,
    SUPPORTED_LANGUAGES,
)
from main import app  # noqa: E402
from services import image_service  # noqa: E402
from services.publish_service import check_language  # noqa: E402

results: list[dict] = []


def check(label: str, ok: bool, detail: object = "") -> None:
    # detail 会进 JSON：bytes 之类的类型先转成可序列化的形式，别让探针自己崩
    if isinstance(detail, (bytes, bytearray)):
        detail = repr(bytes(detail))
    results.append({"label": label, "ok": bool(ok), "detail": detail})


def raises(fn) -> Exception | None:
    """调用 fn，抛错就返回异常，不抛返回 None。"""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - 探针就是要把异常抓下来看
        return exc
    return None


# --- 1. 语言白名单 --------------------------------------------------------
def test_language() -> None:
    all_site_langs = set(chain(*SITE_LANGUAGES.values()))
    supported = set(SUPPORTED_LANGUAGES)
    missing = sorted(all_site_langs - supported)
    check("SITE_LANGUAGES 全部语言都在 SUPPORTED_LANGUAGES 里",
          all_site_langs <= supported,
          {"missing": missing, "siteLanguages": sorted(all_site_langs),
           "supported": list(SUPPORTED_LANGUAGES)})
    check("印尼语 id 已在白名单", "id" in supported, list(SUPPORTED_LANGUAGES))
    # 顺序对齐：白名单里出现的每个键都该有中文标签，否则前端只能显示裸代码
    no_label = [code for code in SUPPORTED_LANGUAGES if code not in LANGUAGE_LABELS]
    check("白名单每项都有中文标签", not no_label, no_label)
    # 白名单顺序与 LANGUAGE_LABELS 的键顺序一致（允许白名单是标签表的子集）
    label_order = [code for code in LANGUAGE_LABELS if code in supported]
    check("白名单顺序与 LANGUAGE_LABELS 对齐",
          label_order == list(SUPPORTED_LANGUAGES),
          {"supported": list(SUPPORTED_LANGUAGES), "labelOrder": label_order})

    # 站点已知：印尼站 + id 必须放行（修复前这里是 400）
    exc = raises(lambda: check_language("id", "ID", allow_mismatch=False))
    check("check_language('id','ID') 不抛", exc is None,
          None if exc is None else f"{type(exc).__name__}: {exc}")

    # 站点已知：id 发到马来站仍然要报站点不匹配（且是 422 SITE_LANGUAGE_MISMATCH）
    exc = raises(lambda: check_language("id", "MY", allow_mismatch=False))
    check("check_language('id','MY') 抛站点不匹配",
          isinstance(exc, errors.AppError)
          and exc.code == errors.SITE_LANGUAGE_MISMATCH
          and exc.status == 422,
          None if exc is None else {"code": exc.code, "status": exc.status,
                                    "message": exc.message})

    # 站点未知：白名单兜底，id 在白名单里 -> 放行 + 返回 warning
    warn = raises(lambda: check_language("id", "UNKNOWN_SITE", allow_mismatch=False))
    check("check_language('id','UNKNOWN_SITE') 不抛", warn is None,
          None if warn is None else f"{type(warn).__name__}: {warn}")
    warning = check_language("id", "UNKNOWN_SITE", allow_mismatch=False)
    check("站点未知时返回显式 warning", isinstance(warning, str) and bool(warning),
          warning)

    # 站点未知 + 谁都不认识的语言 -> 仍然拒绝（白名单兜底还在）
    exc = raises(lambda: check_language("xx", "MY", allow_mismatch=False))
    check("check_language('xx','MY') 仍抛（站点已知但不匹配）",
          isinstance(exc, errors.AppError) and exc.status == 422,
          None if exc is None else {"code": exc.code, "status": exc.status})
    exc = raises(lambda: check_language("xx", "UNKNOWN_SITE", allow_mismatch=False))
    check("check_language('xx','UNKNOWN_SITE') 抛（白名单兜底）",
          isinstance(exc, errors.AppError)
          and exc.code == errors.VALIDATION_ERROR
          and exc.status == 400,
          None if exc is None else {"code": exc.code, "status": exc.status,
                                    "message": exc.message})

    # allow_mismatch 短路在最前面：连白名单都不该跑
    exc = raises(lambda: check_language("xx", "MY", allow_mismatch=True))
    check("check_language('xx','MY',allow_mismatch=True) 不抛", exc is None,
          None if exc is None else f"{type(exc).__name__}: {exc}")
    exc = raises(lambda: check_language("id", "MY", allow_mismatch=True))
    check("allow_mismatch 短路对 id 同样生效", exc is None,
          None if exc is None else f"{type(exc).__name__}: {exc}")
    # 空语言：老行为是放行（不传语言 = 不校验）
    check("空语言不抛", raises(lambda: check_language("", "MY", allow_mismatch=False)) is None)

    # 每个站点用它自己的期望语言都不该被拒——这是本次缺陷的通用形态
    for site, langs in SITE_LANGUAGES.items():
        for code in langs:
            exc = raises(lambda s=site, c=code: check_language(c, s, allow_mismatch=False))
            check(f"站点 {site} 的期望语言 {code} 放行", exc is None,
                  None if exc is None else f"{type(exc).__name__}: {exc}")


# --- 2. 图片密钥验证入口 --------------------------------------------------
def test_image_verify() -> None:
    token = get_settings().local_secret()
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{API_PREFIX}/images/verify"

    # LOCALAPPDATA 已在文件头指向空目录，临时数据目录里也没有 image-key.txt，
    # 因此这台「机器」确实一个密钥都没有
    os.environ.pop("AOHUANG_IMAGE_KEY", None)
    settings = get_settings()
    settings.image_key = ""
    try:
        settings.image_key_path.unlink()
    except OSError:
        pass
    check("前置条件：数据目录里没有 image-key.txt",
          not settings.image_key_path.is_file(), str(settings.image_key_path))
    check("前置条件：老版遗留目录里也没有密钥文件",
          not settings._legacy_image_key(), str(settings.legacy_image_key_dir()))

    with TestClient(app) as client:
        # --- GET：未配置 ---
        resp = client.get(base, headers=headers)
        check("GET /images/verify 200", resp.status_code == 200, resp.status_code)
        body = resp.json()
        check("未配置 configured=false", body.get("configured") is False, body)
        check("未配置 mode 为空串", body.get("mode") == "", body.get("mode"))
        check("未配置 keyHint 为空", body.get("keyHint") == "", body.get("keyHint"))
        message = str(body.get("message") or "")
        check("未配置 message 有内容", len(message) > 20, message[:120])
        check("message 提到文件名 image-key.txt", "image-key.txt" in message, message[:200])
        check("message 提到可放的位置", str(_TMP_DIR) in message, message[:200])
        check("message 提到环境变量兜底", "AOHUANG_IMAGE_KEY" in message, message[:200])

        # --- GET：mock 配置（写一份假密钥进数据目录）---
        fake_key = "sk-probe-0123456789abcdefWXYZ"
        settings = get_settings()
        settings.image_key_path.write_text(fake_key, encoding="utf-8")
        settings.image_key = ""            # 确保走文件而不是环境变量
        resp = client.get(base, headers=headers)
        body = resp.json()
        check("配置后 configured=true", body.get("configured") is True, body)
        check("配置后 mode=direct", body.get("mode") == "direct", body.get("mode"))
        hint = str(body.get("keyHint") or "")
        check("keyHint 长度约 8 字符左右（前后各 4 + 省略号）", 10 <= len(hint) <= 12, hint)
        check("keyHint 是前后各 4 位", hint == f"{fake_key[:4]}...{fake_key[-4:]}", hint)
        check("keyHint 不含完整密钥", fake_key not in json.dumps(body, ensure_ascii=False),
              json.dumps(body, ensure_ascii=False)[:200])
        check("响应体任何字段都不含完整密钥",
              fake_key not in resp.text, resp.text[:200])
        check("如实反映来源是数据目录 image-key.txt",
              "image-key.txt" in str(body.get("source") or ""), body.get("source"))
        check("返回命中的文件路径", str(body.get("path") or "").endswith("image-key.txt"),
              body.get("path"))
        check("已配置 message 提示可做付费验证",
              "POST" in str(body.get("message") or ""), str(body.get("message"))[:160])

        # --- POST：不带 confirm -> 400 且提到 confirm ---
        resp = client.post(base, headers=headers, json={})
        check("POST 不传 confirm -> 400", resp.status_code == 400, resp.status_code)
        err = resp.json().get("error", {})
        check("错误码 CONFIRM_REQUIRED", err.get("code") == errors.CONFIRM_REQUIRED,
              err.get("code"))
        check("message 提到 confirm",
              "confirm" in str(err.get("message") or "").lower(),
              str(err.get("message"))[:160])

        resp = client.post(base, headers=headers, json={"confirm": False})
        check("confirm=false 同样 400", resp.status_code == 400, resp.status_code)

        # --- POST：带 confirm，mock 掉上游，不产生真实费用 ---
        calls: list[dict] = []
        real_submit = image_service._submit

        def fake_submit(raw, size, *, prompt):
            calls.append({"bytes": len(raw), "size": size, "prompt": prompt[:20],
                          "magic": repr(raw[:8])})
            return {"image": None, "taskId": "probe-task-1", "status": "submitted",
                    "upstreamStatus": "processing"}

        image_service._submit = fake_submit
        try:
            resp = client.post(base, headers=headers, json={"confirm": True})
        finally:
            image_service._submit = real_submit

        check("POST confirm=true 200", resp.status_code == 200, resp.status_code)
        body = resp.json()
        check("POST 返回 ok=true", body.get("ok") is True, body)
        check("POST 返回 status", body.get("status") == "submitted", body.get("status"))
        check("POST 返回 reservedMilliyuan=24", body.get("reservedMilliyuan") == 24,
              body.get("reservedMilliyuan"))
        check("POST message 说明验证通过",
              "验证通过" in str(body.get("message") or ""), body.get("message"))
        check("POST 只发了一次上游请求", len(calls) == 1, calls)
        check("POST 冒烟图是合法 PNG 且非 1x1",
              bool(calls) and calls[0]["magic"].startswith("b'\\x89PNG")
              and calls[0]["bytes"] > 100, calls)
        check("POST 响应不含完整密钥", fake_key not in resp.text, resp.text[:200])

        # --- POST：上游失败要如实回报，且抹掉密钥 ---
        def failing_submit(raw, size, *, prompt):
            raise errors.upstream(f"图片服务返回失败：401 Bearer {fake_key}")

        image_service._submit = failing_submit
        try:
            resp = client.post(base, headers=headers, json={"confirm": True})
        finally:
            image_service._submit = real_submit
        body = resp.json()
        check("上游失败 ok=false", body.get("ok") is False, body)
        check("上游失败 status=failed", body.get("status") == "failed", body.get("status"))
        check("上游失败 message 说明是验证失败",
              "验证失败" in str(body.get("message") or ""), str(body.get("message"))[:160])
        check("上游失败响应里不含完整密钥", fake_key not in resp.text, resp.text[:200])

        # --- GET：只认老版遗留文件时，来源要如实指向那个文件 ---
        # 先删掉数据目录那份，否则 image_api_key() 会优先命中它，测不到回退分支
        try:
            settings.image_key_path.unlink()
        except OSError:
            pass
        legacy_dir = settings.legacy_image_key_dir()
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_key = "legacy-probe-key-ABCD9876"
        (legacy_dir / "codesonline-key.txt").write_text(legacy_key, encoding="utf-8")
        resp = client.get(base, headers=headers)
        body = resp.json()
        check("只有老版文件时 configured=true", body.get("configured") is True, body)
        check("来源指向老版遗留 codesonline-key.txt",
              "codesonline-key.txt" in str(body.get("source") or ""), body.get("source"))
        check("来源路径指向老版目录",
              str(body.get("path") or "").endswith("codesonline-key.txt"),
              body.get("path"))
        check("老版密钥也只露前后各 4 位",
              str(body.get("keyHint")) == f"{legacy_key[:4]}...{legacy_key[-4:]}",
              body.get("keyHint"))
        check("老版密钥完整值不出现在响应里", legacy_key not in resp.text, resp.text[:200])
        try:
            (legacy_dir / "codesonline-key.txt").unlink()
        except OSError:
            pass

        # --- 冒烟图本身要能被 _image_type 认成 PNG，否则上游会先拒格式 ---
        detected = image_service._image_type(_smoke_png_for_probe())
        check("冒烟 PNG 能通过 _image_type 魔数判定",
              detected == ("image/png", "png"), detected)

        # --- GET /images/verify 没被动态路由吃掉（路由表里确实挂着静态路径）---
        paths = {getattr(route, "path", "") for route in app.routes}
        check("路由表里有 GET /api/v1/images/verify",
              f"{API_PREFIX}/images/verify" in paths,
              sorted(p for p in paths if "verify" in p))

        # 收尾：删掉假密钥，别留给后续探针
        try:
            settings.image_key_path.unlink()
        except OSError:
            pass


def main() -> int:
    test_language()
    test_image_verify()
    passed = sum(1 for item in results if item["ok"])
    failed = [item for item in results if not item["ok"]]
    payload = {"total": len(results), "passed": passed,
               "failed": len(failed), "items": results}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"TOTAL={len(results)} PASS={passed} FAIL={len(failed)} OUT={OUT}")
    for item in failed:
        print("FAIL:", json.dumps(item, ensure_ascii=False)[:400])
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
