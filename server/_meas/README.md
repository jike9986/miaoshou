# `_meas/` — 回归探针

这里是**手工验证脚本**，不是产品代码。它们的存在理由只有一个：
这个项目有过「改 A 弄坏 B、两周后才发现」的历史，而很多行为（上游接口的
形状、闸门的判定顺序、乐观锁的并发语义）用单元测试表达起来比直接用
`TestClient` 打一遍真实路由更绕。

## 怎么跑

```bash
cd server
python _meas/probe_all.py
```

一次跑完全部 36 个探针，按功能分组汇总，写出 `probe_all.json`。
单跑一个：`python _meas/probe_batch.py`。

## 三条铁律（写新探针必须遵守）

### 1. 必须把数据目录指到临时目录

```python
import os, sys, tempfile
os.environ["AOHUANG_DATA_DIR"] = tempfile.mkdtemp(prefix="probe-xxx-")
os.environ["TK_AUTOMATION_DATA_DIR"] = os.environ["AOHUANG_DATA_DIR"]
# 两个都要设：一个是产品代码读的，一个是打包版读的。少设一个就可能落到真实库。
```

**为什么是铁律**：真实库里有 355 件商品、4 个账号、加密过的 AppSecret。
一个没设数据目录的探针跑一遍，轻则写脏数据，重则该删的删了查不回来。
历史上真出过这种脏探针（`probe_imgtest.py`，已删除）。

### 2. 必须有 `check()` 式断言

```python
RESULTS: list[tuple[str, bool, str]] = []

def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), str(detail)))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
```

最后必须打印一行 `TOTAL <n> PASS <p> FAIL <f>` 并 `sys.exit(0 if f == 0 else 1)`。
`probe_all.py` 就是靠这两样东西统计的。

**为什么**：只有 `print()` 没有断言的脚本是「一次性探查」，它永远 PASS，
红了也没人知道。本目录里曾有 32 个这类脚本，已全部清掉。

### 3. 必须能独立跑

不依赖其它探针先跑过、不依赖上一个探针留下的数据库状态。
每个探针自己建临时库、自己 `init_db()`、自己造需要的数据。

## 第四条铁律（只对并发探针）：必须走真实路由时序

`probe_concurrency.py` 是唯一真开线程撞的探针，它踩过两个静态扫描永远
看不见的坑，写同类探针时必须照做：

1. **每个线程自己的 `SessionLocal()`**。共用会话测不出任何并发问题——
   SQLAlchemy 会话不是线程安全的，共用的结果只是乱，不是「并发」。
2. **必须覆盖「服务函数 → commit」整段**，不能只调服务函数。曾经的缺陷就是
   锁放在服务函数里、`commit` 在路由里：锁先放掉，改动还没提交，第二个线程
   读不到旧行，于是上游被打两次。只调服务函数测不出来（因为不 commit）。
   现在锁是会话级的（`core/locks.py`），探针要按路由时序跑才能证明它活到了 commit。

另外：断言的落点要是**外部可见的事实**，不是「有没有报错」。判据是
`claim_to_shop` 被调了几次、`batches` 表里有几行、`revision` 推进了几格。

## 分组含义

| 组 | 红了代表什么 |
|---|---|
| 核心契约 | 错误信封 / 两步保存 / 只读模式 / 加密 —— 所有接口共用的地基 |
| 定价与模板 | 汇率、模板合并视图、试算公式 |
| 入仓与闸门 | 保存前的十条检查、类目匹配、颜色规范化 |
| 图片链路 | 翻译、去重、公网地址、图床上传、结果回写 |
| 批次与仓库 | 批次状态机、幂等闸、链接池、清理 |
| 参考数据 | 品牌分页、制造商、尺码表、类目树 |
| **多人协作** | **不是功能对不对，是「两个人同时用会不会静默覆盖/双重提交」** |

最后一组单独分开是有意的：它红了说明协作保护漏了，而此时功能可能全部正常 ——
这种 bug 最难被发现，因为单人测试永远是绿的。

## 需要真实上游的探针

少数断言必须打到妙手真实接口才能验（品牌分页的 `nextPageToken`、
类目树的 3013 个节点、`priceTemplateList` 的实际返回）。这些在
`probe_reference.py` / `probe_brand_page.py` 里，需要：
- `userdata/` 下有一个已连接的账号（`accounts` 表里有可用的 AppKey/AppSecret）
- 能连到妙手接口

没有账号时会跳过这几条而不是 FAIL —— 但 CI 里跑不出完整覆盖，
所以**改了上游相关代码要本机手动跑一次**。

## 已删除的一次性脚本（留个记录，别再写回来）

`probe_attr` `probe_cat` `probe_clean` `probe_clean2` `probe_compat` `probe_cov`
`probe_cov2` `probe_dup` `probe_img` `probe_img2` `probe_ing` `probe_leaf_cids`
`probe_one` `probe_ref_meta` `probe_ref_metadata_leaf` `probe_ref_tree_expand`
`probe_ref_tree_raw` `probe_ref_upstream` `probe_save` `probe_sched` `probe_sched2`
`probe_sku` `probe_sku2` `probe_sku3` `probe_trres` `probe_trres2` `probe_upstream`
`probe_v` `probe_w` `probe_words` `probe_wrep` `probe_year` `probe_imgtest`

它们全是「打一次上游、把响应 print 出来看一眼」的探查脚本，零断言，
其中 14 个连临时数据目录都没设。它们验证过的结论已经被正式探针覆盖。
