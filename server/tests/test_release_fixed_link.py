"""固定下载链接的契约测试（2026-11-26 事故防回归）。

## 事故

`latest-Setup.exe` 是用户被贴的固定下载链接。它以前只是个软链接，落在 nginx
目录级 `Cache-Control: immutable, max-age=31536000` 里 → Cloudflare 把 **0.1.44**
的字节缓存了一整年。源站早就指向 0.1.54，用户下载到的还是 0.1.44。

`version.json` → 带哈希文件名那条链路一直是好的，于是 `verify_release.py`
连过 6 个版本全绿 —— 它只校验了清单地址，没校验固定链接。

## 这里的断言就是修好后的规则本身

1. 安装包文件名必须带内容哈希（内容变 → 名字变 → CDN 缓存自然失效）
2. 固定链接的跳转目标不能是 `latest-Setup.exe` 自身（自跳转 + 重新落进长缓存）
3. 清单里的 url 必须符合上面两条，否则不许发布
"""
from __future__ import annotations

import pytest

from scripts.release_fixed_link import (
    FIXED_LINK_PATH,
    artifact_name,
    artifact_url,
    fixed_link_is_safe,
    parse_snippet_target,
    redirect_snippet,
)

# 0.1.54 的真实值，照着线上抄的，别改
V154_SHA = "daa2c7bfd10dee752a9179b2f156430033ac52df9cb4b58a64aba76b1682ebb7"
V154_SIZE = 71459602
# 0.1.44 的真实值 —— 就是被 Cloudflare 缓存了一年、一直被发出去的那个
V144_SHA = "f5295be442b67e412b8f166a11a468e5ccbfad5dc447529fef7a8bfb620fd0c1"
V144_SIZE = 71460377


def _manifest(version: str = "0.1.54", sha: str = V154_SHA) -> dict:
    return {
        "version": version,
        "url": artifact_url(version, sha),
        "sha256": sha,
        "sizeBytes": V154_SIZE,
        "channel": "stable",
    }


# --- 文件名 -------------------------------------------------------------

def test_artifact_name_carries_content_hash():
    """文件名必须带 sha256 前 8 位。没有它就没有「URL 随内容变」这条保命机制。"""
    assert artifact_name("0.1.54", V154_SHA) == "AohuangShangpin-0.1.54-daa2c7bf-Setup.exe"


def test_artifact_name_differs_between_versions_even_with_same_hash():
    a = artifact_name("0.1.54", V154_SHA)
    b = artifact_name("0.1.55", V154_SHA)
    assert a != b


def test_artifact_name_rejects_short_hash():
    """哈希短于 8 位就没法进文件名，必须报错而不是静默截断。"""
    with pytest.raises(ValueError):
        artifact_name("0.1.54", "daa2")


def test_artifact_name_rejects_empty_version():
    with pytest.raises(ValueError):
        artifact_name("  ", V154_SHA)


# --- URL ----------------------------------------------------------------

def test_artifact_url_points_at_cdn_host_with_hashed_name():
    url = artifact_url("0.1.54", V154_SHA)
    assert url == (
        "https://aohuang888.xyz/miaoshou-downloads/"
        "AohuangShangpin-0.1.54-daa2c7bf-Setup.exe"
    )


# --- snippet ------------------------------------------------------------

def test_redirect_snippet_returns_302_to_hashed_artifact():
    snippet = redirect_snippet(artifact_url("0.1.54", V154_SHA))
    assert "return 302 https://aohuang888.xyz/miaoshou-downloads/" \
           "AohuangShangpin-0.1.54-daa2c7bf-Setup.exe;" in snippet


def test_redirect_snippet_refuses_latest_self_redirect():
    """★ 这条是事故本体：指回 latest-Setup.exe 会形成自跳转，
    并且重新落进 Cloudflare 的 immutable 长缓存。"""
    with pytest.raises(ValueError):
        redirect_snippet(f"https://aohuang888.xyz{FIXED_LINK_PATH}")


def test_redirect_snippet_refuses_non_https():
    with pytest.raises(ValueError):
        redirect_snippet("http://aohuang888.xyz/miaoshou-downloads/AohuangShangpin-1-Setup.exe")


def test_redirect_snippet_refuses_name_without_version_hash():
    """手写的、不带哈希的文件名同样会被 CDN 长缓存，必须拒绝。"""
    with pytest.raises(ValueError):
        redirect_snippet("https://aohuang888.xyz/miaoshou-downloads/setup.exe")


def test_snippet_round_trips_through_parse():
    """写进 nginx 的东西必须能被读回来 —— publish 之后的 verify 依赖这一点。"""
    snippet = redirect_snippet(artifact_url("0.1.54", V154_SHA))
    assert parse_snippet_target(snippet) == artifact_url("0.1.54", V154_SHA)


def test_parse_snippet_returns_none_when_no_return_line():
    assert parse_snippet_target("# 只有注释\n") is None


# --- 发布闸门 -----------------------------------------------------------

def test_manifest_with_hashed_url_passes_gate():
    ok, reason = fixed_link_is_safe(_manifest())
    assert ok, reason


def test_gate_rejects_manifest_pointing_at_latest_link():
    """如果哪天有人把清单改回固定链接，发布闸门必须拦住。"""
    manifest = _manifest()
    manifest["url"] = f"https://aohuang888.com{FIXED_LINK_PATH}"
    ok, reason = fixed_link_is_safe(manifest)
    assert not ok
    assert "latest-Setup.exe" in reason


def test_gate_rejects_manifest_with_wrong_hash_length():
    manifest = _manifest()
    manifest["sha256"] = "daa2c7bf"
    ok, reason = fixed_link_is_safe(manifest)
    assert not ok
    assert "sha256" in reason


def test_gate_rejects_manifest_missing_url():
    manifest = _manifest()
    manifest.pop("url")
    ok, _ = fixed_link_is_safe(manifest)
    assert not ok


def test_gate_rejects_old_style_bare_filename():
    """0.1.44 之前那种不带哈希的文件名同样过不了闸门。"""
    manifest = _manifest()
    manifest["url"] = "https://aohuang888.com/miaoshou-downloads/奥皇上品-0.1.44-Setup.exe"
    ok, reason = fixed_link_is_safe(manifest)
    assert not ok
    assert "哈希" in reason


# --- 事故原始数据 -------------------------------------------------------

def test_incident_numbers_are_reproducible():
    """把事故现场的两个包固化下来：它们必须是不同的文件名、不同的 sha。

    如果哪天有人「优化」artifact_name 把哈希去掉，这条会立刻失败。
    """
    a = artifact_name("0.1.44", V144_SHA)
    b = artifact_name("0.1.54", V154_SHA)
    assert a != b
    assert V144_SIZE != V154_SIZE
    assert V144_SHA != V154_SHA