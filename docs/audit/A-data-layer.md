# 审计 A：数据层与迁移

> 审计范围：`server/models/`（22 个模型文件）、`server/db/session.py`（742 行）、`server/migrations/`（26 个迁移 + env.py + 2 个自检脚本）
> 审计方式：**全部结论用可重放脚本实证**，不靠读代码猜。脚本用 `alembic upgrade head` 与 `Base.metadata.create_all` 各建一份临时库，逐表逐列逐索引比对；索引问题用 `EXPLAIN QUERY PLAN` 在 2000 行真实数据上验证。
> 审计时点：2026-11-26，HEAD = `4c9f32d`

---

## 结论摘要

| 级别 | 条数 | 说明 |
|---|---|---|
| **P0** | **3** | 数据正确性 / 生产不可用 / 凭据泄漏 |
| **P1** | **5** | 一致性 / 门禁失效 / 性能 |
| **P2** | **4** | 可维护性 |

### 最严重的 3 条

**A-P0-1 —— `operators.short_id` 有模型无迁移，PostgreSQL 部署 100% 起不来，桌面端唯一约束永久缺失。**
`models/operator.py:57` 声明了 `short_id: Mapped[str | None] = mapped_column(String(8), unique=True)`，但 26 个迁移里**没有任何一条**提到它。引入它的 commit `8cfffe9`（v0.1.21）只带了 `20261114_0001_multi_device_tokens.py`，漏了这一列。
实测后果分两条路径：
- **PostgreSQL**（`db/session.py:646-652` 只校验 head，不跑 `_add_missing_columns`）→ 任何触达 `operators` 的读写都抛 `OperationalError: no such column: operators.short_id`。`serialize()` 在 `api/auth.py:159/220/300/302/497/602`、`api/operators.py:106/167/233` 共 9 处被调用，即**登录流程本身就崩**。
- **桌面 SQLite**（走 `_add_missing_columns`）→ 列能被 `ADD COLUMN` 补上，但**唯一约束永远补不上**。实测老库升级后连插两条 `short_id='123456'` 全部成功。

**A-P0-2 —— CI 主线是红的，三道门禁同时失效，「测试通过」这一前提不存在。**
- `python -m pytest tests -m unit -q` → **4 failed / 78**，全在 `tests/test_price_template.py::TestCrossBorderFreight`。
- `python -m ruff check .` → **Found 40 errors**。
- 迁移检查 `scripts/ci_migration_check.py` → exit 0，但它**把真实的漂移降级成 warning 放行**（见 A-P1-1）。
`.github/workflows/ci.yml:82-89` 用 `set -euo pipefail` 跑前两条，任一非零即构建失败。**当前 main 分支的 CI 不可能是绿的。**

**A-P0-3 —— 仓库根 `.captcha_dom.txt`（936 KB）被 git 跟踪，内含真实 `x5secdata` 风控凭据。**
`.gitignore:94-95` 排除的是 `tools/source_match/.captcha_dom.txt`，**不是仓库根的那份**。实测文件内容含 `x5secdata=xg32a3de2e590ecfc7jaa8d820f8cd1e317d496b14ece58ff5281795521507a-...`，指向 `s.1688.com:443/selloffer/offer_search.h`，可重放。

---

## P0 问题（数据正确性 / 生产不可用 / 凭据泄漏）

### A-P0-1 `operators.short_id` 有模型无迁移：PostgreSQL 起不来，SQLite 唯一约束永久缺失

**证据 1：模型声明了，迁移里没有。**
`server/models/operator.py:55-57`
```python
    # 6 位随机字母数字 ID（大写 + 数字），便于在列表中快速找人。
    # 创建账号时自动生成，唯一约束由数据库保证。
    short_id: Mapped[str | None] = mapped_column(String(8), unique=True)
```
注意注释里的原话：「**唯一约束由数据库保证**」。这句话在当前迁移链下是假的。

`grep -rn "short_id" server/migrations/` → **NOT FOUND**（26 个迁移文件，零命中）。

**证据 2：引入它的 commit 漏了迁移。**
```
$ git log -S "short_id" --oneline -- server/models/operator.py
8cfffe9 v0.1.21: UI improvements and feature updates
```
```
$ git show 8cfffe9 --name-only | grep -i migrations
server/migrations/versions/20261114_0001_multi_device_tokens.py
```
该 commit 同时改了 `models/operator.py`（+7 行，含 short_id）、加了 `models/operator_token.py`，但只写了一条迁移（multi_device + operator_tokens 表），**short_id 这一列被漏掉**。

**证据 3：实证 —— alembic 建出的库 vs models 建出的库，逐列比对。**
用 `alembic upgrade head` 与 `Base.metadata.create_all` 各建一份临时库后比对：
```
==============================================================================
Alembic head schema  vs  models.Base.metadata  ——  差异清单
==============================================================================
[列缺失] operators.short_id  alembic 无此列，模型有: {'type': 'VARCHAR(8)', 'nullable': True, 'default': '', 'pk': False}
[唯一约束缺失] operators 模型有 ('short_id',)，alembic 没有

合计 2 条差异
```

**证据 4：实证 —— PostgreSQL 路径（Alembic 建库）上 ORM 直接崩。**
```
场景 A：Alembic 建出的库上执行 ORM 查询（PostgreSQL 启动路径）
  ✗ 崩溃: OperationalError
    (sqlite3.OperationalError) no such column: operators.short_id

  ✗ INSERT 崩溃: OperationalError
    (sqlite3.OperationalError) table operators has no column named short_id
```
崩溃的调用点（9 处）：
```
api/auth.py:159        "name": operator_service.serialize(operator)["name"],
api/auth.py:220
api/auth.py:300
api/auth.py:302        "emailVerified": operator_service.serialize(operator)["emailVerified"]},
api/auth.py:497
api/auth.py:602
api/operators.py:106   return {"operator": operator_service.serialize(row)}
api/operators.py:167   current_data = operator_service.serialize(current)
api/operators.py:233   return {"operator": operator_service.serialize(row), "token": token}
```
而 `serialize()` 里的取值在 `services/operator_service.py:582`：
```python
        "shortId": row.short_id or "",
```

**证据 5：实证 —— 桌面 SQLite 路径补了列，但补不上唯一约束。**
`db/session.py:105-160` 的 `_add_missing_columns()` 只做 `ALTER TABLE ... ADD COLUMN`，函数自己的 docstring 就写明了（`:129-130`）：
```python
    注意本函数只做 ADD COLUMN：类型变更、删列、加唯一约束、改外键都不会
    被兜住，那几种情况必须写 Alembic 迁移并在发版说明里要求用户升级。
```
实测：手搓一个无 `short_id` 的老 `operators` 表 → 跑 `init_db()` → 列补上了，然后：
```
=== 启动后 operators 结构 ===
['id','name','token','enabled','is_admin','created_at','last_seen_at',
 'username','email','email_verified','password_hash','must_change_password',
 'role','short_id','multi_device']

=== 索引 ===
   (0, 'uq_operators_email_normalized', 1, 'c', 1)
   (1, 'uq_operators_username', 1, 'c', 0)
   (2, 'sqlite_autoindex_operators_2', 1, 'u', 0)
   (3, 'sqlite_autoindex_operators_1', 1, 'u', 0)
                     ↑ 只有 name / token 的 autoindex，没有 short_id

=== short_id 值 ===
[(1, None), (2, None)]

!! 严重：两条相同 short_id='123456' 的记录都插入成功（无唯一约束）
```

**证据 6：`db/session.py` 对 short_id 零兜底。**
`grep -n "short_id" server/db/session.py` → **完全没有任何 short_id 兜底**。
对比：同文件为 `username`（`_ensure_sqlite_operator_username_index`，:533）、`email`（`_ensure_sqlite_operator_email_index`，:515）、`app_key`（`_ensure_sqlite_account_app_key_index`，:163）都写了专门的补偿函数。**short_id 是唯一一个既没迁移、也没补偿函数的唯一约束列** —— 说明作者知道「ADD COLUMN 补不上唯一约束」这个坑，并为此写了 3 个补偿函数，只是漏了第 4 个。

**证据 7：`tests/` 对 short_id 零覆盖。**
```
$ grep -rn "short_id\|shortId" server/tests/
!! 没有任何测试覆盖 short_id !!
```
所以 CI 即使全绿也发现不了。

**证据 8：生成函数本身也有逻辑缺口。**
`services/operator_service.py:33,36-42`
```python
_SHORT_ID_CHARS = string.digits

def _generate_short_id(db: Session) -> str:
    """生成一个唯一的 6 位纯数字 ID。"""
    for _ in range(20):
        sid = "".join(random.choices(_SHORT_ID_CHARS, k=6))
        if not db.query(Operator).filter(Operator.short_id == sid).one_or_none():
            return sid
    return "".join(random.choices(_SHORT_ID_CHARS, k=8))  # 实在碰撞就用 8 位
```
两处问题：
- 注释说「6 位随机字母数字 ID（大写 + 数字）」（`models/operator.py:55`），实际 `_SHORT_ID_CHARS = string.digits` **只有数字**，文档与实现不符。
- 兜底返回 **8 位**，而列宽是 `String(8)` —— 刚好卡满。碰撞 20 次后返回 8 位且**不再查重**，靠唯一约束兜底。但桌面 SQLite 上**没有唯一约束**（见证据 5），所以这条兜底路径在桌面上会静默产生重复 ID。

- **性质：根因。** 不是补丁叠补丁，也不是跷跷板。根因是「模型改了、迁移没跟上」，且恰好落在唯一约束这种 `_add_missing_columns` 补不上的类型上。已有的 3 个 `_ensure_sqlite_*` 补偿函数证明团队理解这个风险模型，只是执行时漏了一个。

- **影响：**
  1. PostgreSQL 生产部署：**启动后第一个登录请求就 500**，9 个 serialize 调用点全部触雷。
  2. 桌面端：`short_id` 全为 NULL，`shortId` 字段返回 `""`；一旦有人补数据或走 8 位兜底就会产生重复 ID。
  3. 任何「用 shortId 在列表里快速找人」的前端逻辑在桌面端实际不可用。

- **根因修复方案：**
  1. 补一条迁移，**同时**加列与唯一约束（列必须可空，否则老库 ADD COLUMN 失败）：
     ```python
     def upgrade():
         with op.batch_alter_table("operators") as batch_op:
             batch_op.add_column(sa.Column("short_id", sa.String(8), nullable=True))
         # 回填后再建唯一索引，避免存量 NULL 冲突（NULL 在唯一索引里互不冲突，可安全建）
         op.create_index("uq_operators_short_id", "operators", ["short_id"],
                         unique=True,
                         sqlite_where=sa.text("short_id IS NOT NULL"),
                         postgresql_where=sa.text("short_id IS NOT NULL"))
     ```
  2. 在 `db/session.py` 加第 4 个补偿函数 `_ensure_sqlite_operator_short_id_index()`，与 `_ensure_sqlite_operator_username_index()` 同构（建索引前先查重、有重复就带可行动的报错中止）。
  3. `_SHORT_ID_CHARS` 改成 `string.digits + string.ascii_uppercase`（与注释一致），或把注释改掉。
  4. 8 位兜底路径加查重，或直接抛错要求人工介入。
  5. 补测试：`test_operators_short_id_unique_index_exists`（断言 `PRAGMA index_list` 里有 `uq_operators_short_id`）+ `test_legacy_db_upgrade_creates_short_id_uniqueness`（老库升级后插重复必须报 IntegrityError）。

---

### A-P0-2 CI 三道门禁同时失效：「测试是绿的」这个前提不成立

**证据 1：pytest 主线红 —— 4 failed / 78。**
```
$ cd server && python -m pytest tests -m unit -q
.........FFFF........................................................... [ 92%]
......                                                                   [100%]
=========================== short test summary info ===========================
FAILED tests/test_price_template.py::TestCrossBorderFreight::test_first_weight_range
FAILED tests/test_price_template.py::TestCrossBorderFreight::test_exactly_first_weight
FAILED tests/test_price_template.py::TestCrossBorderFreight::test_continued_weight
FAILED tests/test_price_template.py::TestCrossBorderFreight::test_weight_rounding
```
失败原因（业务代码改了、测试没改）：
`services/price_template.py:19-25` 当前值：
```python
DEFAULT_FREIGHT_TABLE = {
    "currency": "MYR",
    "firstWeightGrams": 30,
    "firstWeightPrice": 0.06,
    "continuedUnitGrams": 10,
    "continuedUnitPrice": 0.08,
}
```
`tests/test_price_template.py:26-47` 仍硬编码改前的 0.18 / 0.12：
```python
    def test_first_weight_range(self):
        result = cross_border_freight_myr("0.03", TEMPLATE)
        assert result == Decimal("0.18")     # 实际 0.06
```
```
$ git show 70086d6 -- server/services/price_template.py | grep -E "^[+-].*(firstWeightPrice|continuedUnitPrice)"
-    "firstWeightPrice": 0.18,
+    "firstWeightPrice": 0.06,
-    "continuedUnitPrice": 0.12,
+    "continuedUnitPrice": 0.08,
```
实测现值：`0.03kg -> 0.06`、`0.035kg -> 0.14`、`0.05kg -> 0.22`、`1.0kg -> 7.82`。

**证据 2：ruff 40 errors。**
```
$ cd server && python -m ruff check .
Found 40 errors.
[*] 24 fixable with `--fix` (6 unsafe fixes available with `--fix --unsafe-fixes`).
```
分类：
```
     12 W292    No newline at end of file
      9 I001    Import block is un-sorted or un-formatted
      7 UP031   Use format specifiers instead of percent format
      6 E702    Multiple statements on one line (semicolon)
      2 SIM300  Yoda condition detected
      1 F841    Local variable `raised` is assigned to but never used
      1 F401    `json` imported but unused
      1 E501    Line too long (101 > 100)
      1 B905    `zip()` without an explicit `strict=` parameter
```

**证据 3：CI 配置确实会因这两条变红。**
`.github/workflows/ci.yml:78-89`
```yaml
      - name: ruff check
        working-directory: server
        run: python -m ruff check .

      - name: pytest
        working-directory: server
        run: |
          set -euo pipefail
          if [ -d tests ]; then
            python -m pytest tests -m unit -q
```
`set -euo pipefail` + 非零退出 = step 失败 = job 失败。

**证据 4：CI 只跑 `-m unit`，覆盖面极窄。**
```
$ python -m pytest tests -m unit --collect-only -q
tests/test_collect_box_service.py: 9
tests/test_price_template.py: 17
tests/test_schedule_multi_shop.py: 9
tests/test_schedule_service.py: 16
tests/test_site_charge_fields.py: 17
tests/test_warehouse_service.py: 10
```
只有 **6 个文件**带 `pytestmark = pytest.mark.unit`。而 `tests/` 下有 100+ 个测试文件 —— `test_role_permissions.py`（鉴权矩阵）、`test_account_ownership.py`（多租户隔离）、`test_shared_account_task_isolation.py` 这些**真正防越权的测试全部不在 CI 里**。
`server/pyproject.toml:92` 的注释直说了：`"unit: pure function tests, no DB required; CI only runs these"`。

- **性质：根因（治理层）。** 不是某条代码的 bug，而是「质量门禁本身失效」——门禁红了没人管，于是它的存在不再提供任何保护。这比任何单条 P0 都危险：**它让其余所有问题的发现成本从「CI 自动」变成「人工审计」**。

- **影响：**
  1. 任何 PR 都能带着红的 CI 合进 main（事实上现在就是）。
  2. 鉴权/多租户隔离这类**最高价值的测试不在 CI 覆盖内**，regression 可以无声进入生产。
  3. 前述 A-P0-1 若加了正确测试也仍在 CI 之外（因为 tests 里连 short_id 都没测）。

- **根因修复方案：**
  1. **立刻**修 4 个运费测试（对齐 0.06/0.08），让 main 先回到绿；`ruff --fix` 清掉 24 个可自动修的，剩 16 个手改。
  2. **扩大 CI 覆盖面**：把 `test_role_permissions.py`、`test_account_ownership.py`、`test_shared_account_task_isolation.py` 加进 `-m unit`（它们用 TestClient + 临时 SQLite，不依赖外部服务），或新增 `-m "unit or auth"` 档。
  3. 在 CI 里加**漂移检查**（见 A-P1-1 的修复方案），把漂移从 warning 改成 fail。
  4. 加分支保护：CI 红禁止合并。

---

### A-P0-3 仓库根 `.captcha_dom.txt`（936 KB）被 git 跟踪，含可重放的 `x5secdata` 风控凭据

**证据 1：文件被跟踪，且 `.gitignore` 的排除路径对不上。**
```
$ git ls-files | grep captcha_dom
.captcha_dom.txt
$ ls -la .captcha_dom.txt
-rw-r--r-- 1 Administrator 197121 957592 Nov 24 19:58 .captcha_dom.txt
$ du -h .captcha_dom.txt
936K    .captcha_dom.txt
```
`.gitignore:94-95` 排的是**子目录**下的同名文件：
```
tools/source_match/.captcha_dom.txt
tools/source_match/.captcha_page.png
```
**根目录那份没有对应规则**，所以从第一天就被跟踪了。

**证据 2：内容含真实风控凭据。**
```
$ grep -o ".\{150\}a8d820f8cd1e317d496b14ece58ff528.\{150\}" .captcha_dom.txt
_tmd_____/page/feedback?rand=S3WxGHAgAt756EpznwfNzJq2AFA2qBNla3j6EINUS8We9dazM_iKElp8DwVSHZUevpC41Bx7RzivXIj9RnZgdg&amp;x5secdata=xg32a3de2e590ecfc7jaa8d820f8cd1e317d496b14ece58ff5281795521507a-717315356a-770062697abaac3an2201508293765kcapslidev233bd03be3b4a10cbdd99d097e1d2794acda__bx__s.1688.com%3A443%2Fselloffer%2Foffer_search.h
```
`x5secdata` 是阿里系反爬的「已通过验证」凭据，配合同一请求里的 `rand` 可短时间重放。这不是「看上去像 token 的字符串」——它带完整的 `s.1688.com:443/selloffer/offer_search.htm` 目标路径，是从真实已通过验证的会话里 dump 出来的。

同时文件里有 31 处 `Cookie`、2 处 `x5sec` 调用点（反爬 SDK 代码），说明这是**整页 DOM**转储，含运行时会话上下文。

**证据 3：同类文件还有 8 个在仓库根。**
```
.captcha_dom.txt   957592
.captcha_page.png   62458
.c1_home.png        66108
.c2_search.png      66344
.c3_page.html      223019
.diag.png           66325
.kw_page.html      419232
.upload_page.png    73601
.img5_a.png        489140
.img5_b.png        104685
```
（`git ls-files` 可见，均在根目录。）

- **性质：根因。** `.gitignore` 是按「我当初在哪个目录下跑脚本」写的，不是按「哪些文件类型永远不该进仓库」写的。脚本后来被移到仓库根跑，规则就全部失效了 —— 这是**规则的作用域与被保护对象的位置解耦失败**。

- **影响：**
  1. 任何拿到仓库的人（含 fork、含 CI 日志、含误公开）都能重放这些风控凭据。
  2. 936 KB × N 个版本的 git 历史膨胀；`git clone` 变慢。
  3. 若 `.captcha_dom.txt` 被误打进 PyInstaller sidecar（`server.spec` 未排除根目录），还会进安装包。

- **根因修复方案：**
  1. 把规则改成**按文件名模式全局匹配**，而不是按目录：
     ```gitignore
     # 反爬/调试转储：任何位置都不进仓库
     .captcha_*
     .diag.*
     .kw_*
     .c1_*  .c2_*  .c3_*
     .img*
     .upload_*
     .tun.*  .school_tun.*  .login.err
     ```
  2. `git rm --cached` 掉这 10 个文件（保留本地），然后**考虑用 `git filter-repo` 清理历史**——凭据一旦进过历史，`git rm` 不解决泄漏。
  3. 立即在 1688 侧作废该会话（重新登录一次使 `x5secdata` 失效）。
  4. 加一条 pre-commit hook 拦「根目录出现 `.*.txt` / `.*.png` / `.*.html`」。

---

## P1 问题

### A-P1-1 `ci_migration_check.py` 把真实的约束漂移降级为 warning 放行，且它根本不做列级比对

**证据 1：CI 迁移检查「通过」了，但缺失的唯一约束只打警告。**
```
$ cd server && python scripts/ci_migration_check.py
[1] upgrade head 后业务表数量：23（metadata 里 23 张）
     account_bindings / accounts / audit_logs / ... / violation_words
[2] 索引：库里显式 47 / 模型 Index 48；autoindex 20 / 期望 21（唯一约束 16 + 字符串主键 5）
    外键：库里 24 / 模型 24
[3] downgrade base 后剩余业务表：0（应为 0）

[结论] 迁移检查通过（upgrade 建 23 表 / 索引与外键齐全 / downgrade 清空）
EXIT=0
```
注意 `[2]` 里 **47 != 48、20 != 21** —— 这正是 `short_id` 缺列缺索引导致的差 1。但代码把它降级成 stderr 警告，不计入 `failures`：
`scripts/ci_migration_check.py:128-138`
```python
    if explicit_indexes != model_indexes:
        # 部分索引（带 WHERE 子句）在 SQLite 里计数与 PostgreSQL 不同，警告即可。
        print(f"    ⚠ 显式索引 {explicit_indexes} != 模型 Index {model_indexes}"
              f"（部分索引 SQLite 计数差异，警告）",
              file=sys.stderr)
    if auto_indexes != expected_autoindexes:
        print(f"    ⚠ autoindex {auto_indexes} != 期望 {expected_autoindexes}"
              f"（部分索引 SQLite 计数差异，警告）",
              file=sys.stderr)
    if fk_count != model_fks:
        failures.append(f"外键 {fk_count} != 模型 {model_fks}，迁移漏建外键")
```
**只有外键差异进 `failures`**，索引/唯一约束差异全部只警告。而 A-P0-1 恰好在「唯一约束」这一类里。

**证据 2：这个「声音被压掉」的注释本身是错的。**
注释说「部分索引（带 WHERE 子句）在 SQLite 里计数与 PostgreSQL 不同」——但 CI 只在 **SQLite 临时库**上跑（`ci_migration_check.py:28-31` 覆盖 `AOHUANG_DATA_DIR` 为 `mkdtemp`），根本不存在「PostgreSQL 计数不同」这一说。这条注释把「一个真实的差 1」解释成了「跨方言的已知噪音」，于是**每次都在同一个地方瞎掉**。

**证据 3：缺少列级比对 —— 这是最大的漏洞。**
`ci_migration_check.py` 全程只比：表集合、显式索引**数量**、autoindex **数量**、外键**数量**。
**它从不比「某个具体的列是否存在」**，也不比「某个具体名字的索引是否存在」。
所以 `operators.short_id` 缺列这件事，在这个脚本里唯一的表现就是「索引数差 1」，然后被警告吞掉。

对比：`migrations/_verify_baseline.py` **做对了** —— 它逐表逐列比 `[type, notnull, default, pk]`、逐索引比名字+unique+origin、逐外键比 `from->table.to|ondelete`：
```
$ cd server && python migrations/_verify_baseline.py
[operators] 列集合不同
  只在 create_all：['short_id']
  只在迁移：      []
[operators] 索引不同
  只在 create_all：['sqlite_autoindex_operators_4|unique=True|origin=u', 'uq_operators_email_normalized|unique=True|origin=c']
  只在迁移：      []
[schedules.completed] create_all=['BOOLEAN', True, None, False] 迁移=['BOOLEAN', True, '0', False]
[schedules.completed_runs] create_all=['INTEGER', True, None, False] 迁移=['INTEGER', True, "'0'", False]

表数量：create_all=23 / 迁移=23
索引数量：create_all=69 / 迁移=67

[结论] 存在差异，见上方逐条输出
EXIT=1
```
**它准确地指出了 `short_id` 和 `schedules` 默认值两处漂移，退出码 1。**
但它的 docstring 自己写着（`_verify_baseline.py:10-12`）：
```python
这个脚本是「基线迁移没漏东西」的证据生成器，不参与应用运行，也不需要进 CI
（CI 里已经有 upgrade head / downgrade base 的冒烟）。留着是为了下次改
models 之后能一条命令复核。
```
—— **正确的检查器被明确排除在 CI 之外，CI 里跑的是一个只数数量、把差异降级成警告的版本。**

**证据 4：它同时暴露了 `uq_operators_email_normalized` 在 Alembic 链上也不存在。**
`_verify_baseline` 指出 `uq_operators_email_normalized` 只在 create_all 侧存在。用逐版本追查确认了是哪一条迁移弄丢的：
```
20261102_1002    -> HAS    [..., 'uq_operators_email_normalized']   [batch反射跳过警告=False]
20261103_1005    -> HAS    [...]                                    [batch反射跳过警告=True]
20261103_1006    -> HAS    [...]
20261103_1007    -> HAS    [...]
20261103_1008    -> HAS    [...]
20261103_1009    -> HAS    [...]
20261103_1010    -> HAS    [...]
20261114_0001    -> MISSING ['ix_operators_enabled','ix_operators_name',
                             'sqlite_autoindex_operators_1..3']      [batch反射跳过警告=True]
20261119_0001    -> MISSING [...]
```
**根因**：`20261114_0001_multi_device_tokens.py:26-30` 用 `op.batch_alter_table("operators")` 加 `multi_device` 列。SQLite 的 batch 模式会「建新表→拷数据→改名」，而**重建时不会复制原表上 SQLAlchemy 无法反射的表达式索引**——alembic 自己就报警了 `SAWarning: Skipped unsupported reflection of expression-based index uq_operators_email_normalized`，但它只是 warning，迁移照常「成功」。
于是：**新库（Alembic 建）没有邮箱唯一性；老库（桌面 create_all + 补偿函数）有。**
桌面端靠 `_ensure_sqlite_operator_email_index()`（`db/session.py:515-530`）用裸 SQL 重建了它，所以桌面是对的；**PostgreSQL 生产库没有这个兜底函数，唯一的邮箱唯一性约束在升级过程中被静默丢弃了**。

- **性质：跷跷板（局部修 A 坏 B）。** `_ensure_sqlite_*` 系列函数修好了桌面端（A），但同一时刻 Alembic 链在 PostgreSQL 上把约束弄丢了（B）。两个数据库、两套 schema 演化路径、只有一套有补偿 —— 这是结构性的。

- **影响：**
  1. **PostgreSQL 生产库上 `operators.email` 无唯一约束**。`operator_service._email_exists()`（:391-395）只在应用层查重，并发下可穿透 → 同一邮箱可注册两个账号 → **账号找回/邮箱验证码登录可被抢注**。
  2. 迁移漂移在未来会持续发生，且 CI 结构上不可能发现。

- **根因修复方案：**
  1. **把 `migrations/_verify_baseline.py` 提进 CI**，替换或叠加 `ci_migration_check.py`。它已经是对的，只需要改 `ci.yml` 加一行。这是成本最低、收益最大的一步。
  2. 修 `ci_migration_check.py:128-138`：索引/autoindex 差异进 `failures`；如确有部分索引噪音，改成**逐个具名索引比对**而不是比数量。
  3. 补一条迁移重建 `uq_operators_email_normalized`（因为 `20261114_0001` 已发布，只能正向补）：
     ```python
     def upgrade():
         op.create_index("uq_operators_email_normalized", "operators",
                         [sa.text("lower(trim(email))")], unique=True,
                         sqlite_where=sa.text("email IS NOT NULL AND trim(email) <> ''"),
                         postgresql_where=sa.text("email IS NOT NULL AND trim(email) <> ''"))
     ```
  4. 修 `20261114_0001` 的写法（对未发布的库）：batch 重建前先把表达式索引 dump 成 SQL 文本、重建后再 `exec_driver_sql` 回放 —— `db/session.py:404-429` 的 `_ensure_sqlite_product_key_scope()` 已经是这个写法，可以照抄。
  5. 加一条**永久性**回归测试：断言 Alembic 建出的库上 `PRAGMA index_list('operators')` 含 `uq_operators_email_normalized`。

---

### A-P1-2 `schedules.completed` / `completed_runs` 默认值双侧漂移，create_all 库上原生 INSERT 直接失败

**证据 1：模型只有 Python 端 default，迁移只有 server_default。**
`models/schedule.py:52-53`
```python
    completed: Mapped[bool] = mapped_column(default=False)
    completed_runs: Mapped[int] = mapped_column(Integer, default=0)
```
注意两列都是 `nullable=False` 的隐式（`Mapped[bool]` 非 Optional → NOT NULL），但**都没有 `server_default`**。

`migrations/versions/20261103_1010_schedule_product_selection.py:17-18`
```python
        batch_op.add_column(sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("completed_runs", sa.Integer(), nullable=False, server_default="0"))
```
迁移给了 `server_default`，模型没给。

**证据 2：实证 —— 建库时 PRAGMA 默认值不同。**
```
=== alembic upgrade head ===
  completed            dflt='0'
  completed_runs       dflt="'0'"

=== create_all ===
  completed            dflt=None
  completed_runs       dflt=None
```

**证据 3：实证 —— create_all 库上任何绕过 ORM 的 INSERT 都失败。**
```
=== alembic upgrade head ===
  原生 INSERT 后: [(0, 0)]

=== create_all ===
  ✗ 原生 INSERT 失败: IntegrityError: NOT NULL constraint failed: schedules.completed
```
这不是理论问题：`server/scripts/migrate_sqlite_to_postgres.py` 用原生 SQL 搬数据；任何运维脚本、BI 工具、`sqlite3` 手工修数据都会踩。

**证据 4：这是一整类问题，不止这两列。**
`models/schedule.py` 里所有非 Optional 列都只有 Python default：
```
line 21: name           default=""        ← NOT NULL, 无 server_default
line 22: enabled        default=True
line 29: shop_id        default=""
line 30: template_name  default=""
line 39: frequency      default="daily"
line 41: days           default=list
line 44: start_date     default=""
...
```
而基线迁移（`20261028_2020_..._baseline_existing_13_tables.py:189-217`）把这些全建成 `nullable=False` 且**无 server_default**。所以「Alembic 建表 = create_all 建表」在**所有 default 列上都不等价**，只是恰好 `20261103_1010` 给这两列加了 server_default，才让不一致变得可见。

- **性质：根因（且是补丁叠补丁的产物）。** 根因是「模型的 default 与迁移的 server_default 被当成同一件事」。`20261103_1010` 之所以给 server_default，是因为 `batch_op.add_column` 给已有表加 NOT NULL 列**必须**有 server_default（否则老库补列失败）——这是 SQLite 的硬限制逼出来的，不是刻意设计。于是同一个列在不同建库路径下语义不同。

- **影响：**
  1. create_all 路径（桌面端）建出的库，原生 SQL 写入会失败。
  2. `docs/migrations.md:256` 写着「改模型，**必须给 `server_default`**」，但模型里大量列没遵守 —— 文档与代码不一致。
  3. 更隐蔽的：任何 `migrate_sqlite_to_postgres.py` 的列默认值推断都可能错。

- **根因修复方案：**
  1. 给 `models/schedule.py` 这两列补 `server_default`：
     ```python
     completed: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
     completed_runs: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
     ```
  2. **系统性**地给所有非 Optional 列补 `server_default`，与迁移对齐；或改 `CONTRIBUTING.md` 的规范为「非 Optional 列必须同时给 `default` 和 `server_default`」（`models/product.py:118-119` 的 `revision` 就是正确示范：`default=1, server_default="1", nullable=False`）。
  3. 加测试：`test_model_server_defaults_match_migrations`，遍历 `Base.metadata` 断言每个非 Optional 列都有 `server_default`。

---

### A-P1-3 索引策略：四个高频列表筛选条件全部走全表扫描，其中三个是 JSON 内部字段

**证据 1：`products` 表现有索引只有 4 条。**
`models/product.py:127-133`
```python
    __table_args__ = (
        UniqueConstraint("account_id", "detail_id", "shop_id", name="uq_products_identity"),
        Index("ix_products_category", "category_id"),
        Index("ix_products_group", "group_id"),
        Index("ix_products_status", "status"),
        Index("ix_products_saved", "saved_at"),
    )
```
实测建出的索引：
```
  sqlite_autoindex_products_1     ← uq_products_identity
  ix_products_category            CREATE INDEX ix_products_category ON products (category_id)
  ix_products_status              CREATE INDEX ix_products_status ON products (status)
  ix_products_group               CREATE INDEX ix_products_group ON products (group_id)
  ix_products_saved               CREATE INDEX ix_products_saved ON products (saved_at)
```

**证据 2：实证 —— 五个真实查询模式的 EXPLAIN QUERY PLAN（2000 行 products）。**
```
--- ① 仓库列表主页（automation_result 筛选 + savedDesc 排序 + 分页）
      SCAN products USING INDEX ix_products_saved
      匹配 20 行

--- ② 仓库标题搜索（LIKE '%kw%' —— 前缀通配，B树索引用不上）
      SCAN products
      匹配 2000 行

--- ③ 货源筛选（payload JSON 内部字段）
      SCAN products
      匹配 2000 行

--- ④ 规格核验筛选（quality_review JSON 内部字段 vs revision 列）
      SCAN products
      匹配 667 行

--- ⑤ 学员认领过滤（correlated EXISTS on product_claims）
      SCAN p
      CORRELATED SCALAR SUBQUERY 1
      SEARCH c USING COVERING INDEX sqlite_autoindex_product_claims_1 (product_id=? AND owner_operator_id=?)
```
① 靠 `ix_products_saved` 走索引扫描（因为排序键命中），但 `automation_result` 筛选仍需逐行过滤。
②③④ 是**确定的 `SCAN products`**，无任何索引可用。

**证据 3：对应的真实 SQL 来源。**
`services/warehouse_service.py:713` —— 所有仓库查询的起点：
```python
    query = active_query(db).filter(Product.automation_result.is_(False))
```
`active_query` 在 `:649-650`：
```python
def active_query(db: Session):
    return db.query(Product).filter(Product.deleted_at.is_(None))
```
→ **`automation_result` 与 `deleted_at` 是两个必用过滤列，都没有索引。**

`services/warehouse_service.py:864-869` —— 标题搜索：
```python
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        query = query.filter(or_(Product.title.ilike(pattern, escape="\\"),
                                 Product.detail_id.like(pattern, escape="\\"),
                                 Product.category_name.ilike(pattern, escape="\\")))
```
`%...%` 前缀通配 —— **任何 B 树索引都用不上**，SQLite 与 PostgreSQL 都一样。`title` 是 `Text` 类型，PostgreSQL 上还没有 trigram 索引。

`services/warehouse_service.py:720-746` —— 货源筛选，注释自己解释了为什么必须留在 SQL 层：
```python
        # 为什么用 json_extract 而不是取出来在 Python 里筛：这要参与分页，
        # 内存里筛会让 total 和页数全部错位。SQLite 与 PostgreSQL 都支持
        # json_extract（PG 上是 -> 的语法糖），一份表达式两边都能跑。
        if db.bind.dialect.name == "postgresql":
            _url = func.coalesce(
                Product.payload.op("->>")("sourceUrl"),
                Product.payload.op("->>")("sourceItemId"),
            )
        else:
            _url = func.coalesce(
                func.json_extract(Product.payload, "$.sourceUrl"),
                func.json_extract(Product.payload, "$.sourceItemId"),
            )
```
判断完全正确（必须 SQL 层筛），但**没有为它建任何索引**。`payload` 是 JSON 列，`json_extract(payload, '$.sourceUrl')` 在 SQLite 上无法索引（除非建 `CREATE INDEX ... ON products(json_extract(payload,'$.sourceUrl'))` 表达式索引）。

`services/warehouse_service.py:714-717` —— 规格核验筛选，跨 JSON 与列比较：
```python
    reviewed_revision = Product.quality_review["verifiedRevision"].as_integer()
    if quality_status == "verified":
        query = query.filter(Product.quality_review.is_not(None),
                             reviewed_revision == Product.revision)
```
`json_extract(quality_review,'$.verifiedRevision') = revision` —— **JSON 字段与列跨类型比较，语义上就不可能走索引**（表达式索引也不行，因为是两列比较）。

**证据 4：`automation_run_items` 的 `publish_status` 无索引。**
`services/automation_run_service.py:551/660/771` 三处都是 `Product.automation_result.is_(True)` 起点，同样全表扫。

- **性质：根因（设计层）。** 不是「忘了加索引」，而是「**把可筛选的业务字段放进了 JSON**」，导致物理上无法索引。`payload.sourceUrl`、`quality_review.verifiedRevision` 都是**列表页的筛选条件**，却存在 JSON 里。

- **影响：**
  1. products 2000 行时已全表扫；生产 355 件 + 持续增长，每次列表页刷新触发 3-4 次全表扫。
  2. `query.count()`（`warehouse_service.py:872`）在分页前先跑一次完整扫描，等于**每页请求扫两遍**。
  3. PostgreSQL 上 `payload->>'sourceUrl'` 无索引，50 万行后会成为主要瓶颈。
  4. `%kw%` 搜索在 PostgreSQL 上无 pg_trgm，`ilike` 全表扫且不走并行。

- **根因修复方案：**
  1. **把可筛选字段提升为真实列**（这是唯一治本的）：`payload.sourceUrl` → `products.source_url VARCHAR(1024)`；`quality_review.verifiedRevision` → 已在 `draft_verified_revision` 有列，应改用后者。`weight` 同理（见 A-P1-4）。
  2. 立即可做的索引：
     ```sql
     CREATE INDEX ix_products_active ON products(automation_result, deleted_at, saved_at DESC);
     CREATE INDEX ix_products_account ON products(account_id, automation_result);
     ```
  3. 标题搜索：SQLite 建 FTS5 虚拟表；PostgreSQL 装 `pg_trgm` 并建 `CREATE INDEX ... USING gin (title gin_trgm_ops)`。
  4. `automation_run_items` 补 `Index("ix_automation_run_items_publish_status", "run_id", "publish_status")`。

---

### A-P1-4 `weight` 只存在 `payload` JSON 里：无列可查、无法索引、无法约束

**证据 1：`weight` 在 `products` 表上没有任何列。**
`models/product.py` 全文 133 行，列清单里没有 `weight`。`products` 只有 `price_min`/`price_max`/`stock`/`sku_count` 等数值列。

**证据 2：所有 weight 读写都走 payload dict。**
`services/publish_service.py:1041-1043`
```python
def _weight_of(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("weight")
    return str(value) if value not in (None, "") else None
```
`services/publish_service.py:953-969` —— 品类上限裁剪，直接改 dict：
```python
        raw_weight = item.get("weight")
        try:
            weight_value = float(raw_weight)
        except (TypeError, ValueError):
            weight_value = None
        if weight_value is not None and weight_value > 0:
            cat_name = product_service.category_label(item)
            _dims, cap = product_standard.category_spec(cat_name)
            if weight_value > cap:
                item["weightBeforeCap"] = weight_value
                item["weight"] = cap
```
注意 `float(raw_weight)` 的 `try/except (TypeError, ValueError)` —— 因为 JSON 里存的可能是 `"0.1"`（字符串）或 `0.1`（数字）或 `None`，**类型不确定，只能运行时兜**。这正是「无 schema 校验」的直接后果。

**证据 3：weight 的合法性校验分散在 4 个文件、7 处，各算一遍。**
```
services/product_service.py:180     check(not info.get("weight") or number(info.get("weight")) <= 0, ...)
services/product_service.py:196     check(number(sku.get("weight")) <= 0, "missing_sku_weight", ...)
services/product_service.py:249     "weight": _text_or_none(info.get("weight")),
services/product_standard.py:239    weight = _num(data.get("weight"))
services/product_standard.py:482-506  (4 个分支分别改 data["weight"])
services/price_template.py:775-819   weight = result.get("weight") / sku.get("weight")
services/publish_service.py:1041     payload.get("weight")
services/readback_check.py:373-379   _same_number(actual.get("weight"), payload.get("weight"))
```
`services/product_standard.py:482-506` 的裁剪逻辑：
```python
    weight = _num(data.get("weight"))
    ...
    sku_weights = [_num(s.get("weight")) for s in active]
    if sku_weights:
        data["weight"] = round(median(sku_weights), 2)
        weight = data["weight"]
        data["weight"] = max(WEIGHT_MIN, min(0.6, cap))
        weight = data["weight"]
    ...
        data["weight"] = WEIGHT_MIN
    ...
        data["weight"] = cap
```
—— 同一个「重量该是多少」的概念，在 `product_standard`（裁剪）、`publish_service`（品类 cap）、`price_template`（计费）**三处各算一遍，口径不同**。

**证据 4：没有任何地方能按 weight 查询或统计。**
```
$ grep -rn "weight" server/api/ | grep -v __pycache__
→ 只有 system.py 的 weightIncrement/weightLimit 设置项，无任何按 weight 过滤的端点
```
运营想「筛出所有重量填错的商品」在当前 schema 下**做不到**，只能全表读出来在 Python 里算 —— 而 `payload` 是 JSON，`db.query(Product)` 会把每行的 payload 全部反序列化。

- **性质：根因（schema 设计）。** weight 是**参与计价的核心数值**，却与「备注、原始上游 JSON」混在同一个 `payload` blob 里。

- **影响：**
  1. 无法建索引 → 无法按重量筛选/排序/聚合。
  2. 无法加 CHECK 约束 → 负数、0、超大值只能靠应用层 7 处各兜一遍，必漏。
  3. 类型不确定（str/float/None 混存）→ 每个读点都要 `try/except`。
  4. 计价链路 3 处重复实现 → 改一处漏两处（`rules.md` 的 Pacer 事故就是同类）。
  5. `payload` 是 `JSON` 大字段，每次列表查询都要整段反序列化，无谓 IO。

- **根因修复方案：**
  1. 加列迁移：
     ```python
     # products
     weight_kg: Mapped[float | None] = mapped_column(Float)          # 已裁剪的最终重量
     weight_raw_kg: Mapped[float | None] = mapped_column(Float)      # 上游原值（+ weightBeforeCap 同源）
     ```
     加 `CheckConstraint("weight_kg IS NULL OR (weight_kg > 0 AND weight_kg <= 5)")`。
  2. 一次性回填：`UPDATE products SET weight_kg = json_extract(payload,'$.weight')`（SQLite）/ `(payload->>'weight')::float`（PG）。
  3. 把 `publish_service` / `product_standard` / `price_template` 三处裁剪逻辑合并到 `product_standard.resolve_weight()` 单一函数。
  4. `payload` 保留为「上游原始快照，只写不读」——一旦有读点就说明该字段该升列。

---

### A-P1-5 `_seed_settings()` 嵌套事务：`session_scope` 已 commit，函数内又 commit 一次

**证据：**
`db/session.py:697-707`
```python
def _seed_settings() -> None:
    from models.setting import DEFAULT_SETTINGS, Setting

    with session_scope() as db:
        existing = {row.key for row in db.query(Setting).all()}
        for key, value in DEFAULT_SETTINGS.items():
            if key in existing:
                continue
            db.add(Setting(key=key, value=value))
        _fix_workflow_defaults(db)
        db.commit()          # ← session_scope 退出时还会再 commit 一次
```
`session_scope`（`:92-102`）：
```python
@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()          # ← 这里
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
```
`db.commit()` 之后再 `db.commit()` 是空操作（SQLAlchemy 允许），**本身不报错**。真正的问题是语义混乱：`_seed_settings` 的 `db.commit()` 让「这个 with 块是原子的」这个假设失效 —— 若后续 `_fix_workflow_defaults` 抛异常，前面的 `db.add` 已经被外层 commit 提交了。这里恰好 `_fix_workflow_defaults` 在 commit 之前调用，所以现在没炸，但**顺序一改就是静默的部分提交**。

同类：`db/session.py` 里 6 处 `.commit()`，其中 `_seed_settings`（:707）、`_backfill_sqlite_account_bindings`（:603 的 `engine.begin()` 隐式 commit）、`_ensure_sqlite_multi_account_bindings`（:332/:358 手工 commit + `PRAGMA foreign_keys=OFF` 需要在事务外）—— 这三种事务风格混在同一个文件里。

- **性质：补丁叠补丁。** `session_scope` 是后来加的统一封装（本意是「别每个调用点自己写 try/commit/rollback」），但旧的 `_seed_settings` 没删掉自己的 `db.commit()`。

- **影响：** 目前无害（commit 幂等），但这是「哪一层负责提交」不清的具体表现，会诱导后续维护者在任意位置加 commit。

- **根因修复方案：** 删掉 `_seed_settings` 里的 `db.commit()`（`session_scope` 已负责）。加一条 lint/评审规则：「`with session_scope()` 块内不允许出现 `db.commit()`」。

---

### A-P1-6 事务边界：229 处手工 `.commit()`，服务层与路由层各自提交，无法原子组合

**证据 1：commit 分布（生产代码，排除 tests/_meas）。**
```
$ grep -rn "\.commit()" --include=*.py api services db core models | grep -v __pycache__ | wc -l
229
```
按文件：
```
     32 api/auth.py
     23 api/warehouse.py
     18 api/operators.py
     14 api/system.py
     14 api/collect_box.py
     10 api/size_charts.py
     10 api/images.py
     10 api/batch.py
      9 services/claim_job_service.py
      8 services/publish_job_service.py
      8 api/schedules.py
      7 services/automation_run_service.py
      7 api/words.py
      6 db/session.py
      6 api/publish.py
      6 api/groups.py
      6 api/carrier.py
      5 services/login_rate_limit_service.py
      4 api/titles.py
      3 services/schedule_service.py
      3 services/collect_box_service.py
      3 api/publish_jobs.py
      3 api/categories.py
      2 services/publish_service.py
      ...
      1 core/locks.py
```

**证据 2：`warehouse_service.py`（4551 行，最大服务）自己 0 commit —— 这是对的。**
```
$ grep -c "\.commit()" services/warehouse_service.py
0
```
所有 commit 在 `api/warehouse.py`（23 处）。**这是正确的分层**：服务层只 flush，路由层决定事务边界。

**证据 3：但 `carrier_service.py`（2322 行）也是 0 commit，而 `claim_job_service` / `publish_job_service` 却自己 commit。**
```
$ grep -c "\.commit()" services/carrier_service.py
0
$ grep -c "\.commit()" services/claim_job_service.py
9
$ grep -c "\.commit()" services/publish_job_service.py
8
$ grep -c "\.commit()" services/automation_run_service.py
7
```
—— **同一个 services/ 目录里两种截然相反的事务契约并存**。调用方无法从签名判断「我调完这个函数，数据是不是已经落盘了」。

**证据 4：具体的「上层想回滚，下层已提交」现场。**
`services/claim_job_service.py:445-461`
```python
                result = publish_service.publish(
                    db, body, _log_operator_id=batch.operator_id, _claim_progress=report)
                succeeded, failure_messages, known_outcome = _claim_chunk_outcome(
                    result, chunk)
                if not known_outcome:
                    stop_after_chunk = True
                    logger.warning(...)
                db.commit()                                    # :454
            except Exception as exc:
                db.rollback()                                  # :456
```
`publish_service.publish()` 内部在 `services/publish_service.py:451` 和 `:467` 有 2 处 `db.commit()`：
```python
451:    db.commit()
467:        db.commit()
```
所以：`publish()` 返回时**上游副作用（妙手已复制商品）+ 本地 publish_logs 已落盘**。此时若 `_claim_chunk_outcome` 抛异常或 `db.commit()`（:454）失败，`db.rollback()`（:456）**回滚不掉任何已提交的东西**。
代码用 `unknown_message = "处理失败或结果未知，请先在采集箱核对"`（:459）、`stop_after_chunk = True`（:460）来应对 —— 这是**正确但被迫的**降级：它承认了「无法自动恢复，必须人工核对」。这个降级本身就是「事务边界不可组合」的直接代价。

`services/publish_job_service.py:417-419` 同一模式：
```python
                result = publish_service.publish(
                    db, body, _log_operator_id=batch.operator_id)
                db.commit()
```

**证据 5：`publish_job_service` 在一个循环里 commit 了 5 次，中间任何一步失败都会留下半成品。**
```
:358  db.commit()    ← pending→running 的状态切换
:369  db.commit()    ← 无计划时置 failed
:383  db.commit()    ← 写初始 progress
:397  db.commit()    ← 逐件置 running（每店一次）
:419  db.commit()    ← publish() 之后
:502  db.commit()    ← 每店结果的 progress
:517  db.commit()    ← 批次终态
:527  db.commit()    ← except 分支里的补救
```
`db.refresh(batch)` / `batch_service.get(db, batch_id)` / `db.expire_all()` 在 :361/:456/:504 反复重新读取，说明作者清楚「entity 可能已被外部改过」—— 这是在用「频繁重读 + 频繁提交」规避长事务，代价是**没有原子性可言**。

- **性质：跷跷板（结构性）。** 「服务层不 commit」（warehouse/carrier）让上层能原子组合，但要求服务层不产生外部副作用；「服务层自己 commit」（claim_job/publish_job）能承受外部副作用，但上层失去原子性。项目里**两种都用了**，而 `publish()` 是有外部副作用的那个 —— 它两边都占。

- **影响：**
  1. 任何「认领/发布」操作失败后都处于**不可自动恢复**状态，必须人工核对（代码注释反复承认这点）。
  2. 无法实现「全批成功或全批回滚」的业务语义。
  3. 事务契约不一致 → 新写代码必选错。

- **根因修复方案：**
  1. **确立单一契约并写进 `rules.md`**：服务层**永不** commit；commit 只允许在 `api/` 层和「后台任务入口」（`*_job_service.run()` 这类顶层函数）出现。
  2. `publish_service.publish()` 去掉 2 处内部 commit，改为 flush；由调用方（`api/publish.py` 或 `*_job_service.run()`）提交。
  3. 对有外部副作用的路径显式建模成**状态机 + 补偿**，而不是靠 rollback：
     ```
     pending → dispatching(已发上游) → confirmed / unknown
     ```
     `unknown` 状态下不允许自动重试（现有 `stop_after_chunk` 逻辑保留），但要把「未知」作为**一等状态**存进 `batch_items.status`，而不是靠 message 字符串。
  4. 加守护：`grep "db.commit()" services/*.py` 在 CI 里断言只有白名单文件（`*_job_service.py` 的 `run()`）可出现。

---

## P2 问题

### A-P2-1 `db/session.py` 里 9 个 `_ensure_sqlite_*` 补偿函数 vs Alembic 迁移：两套 schema 演化逻辑无对照表

**证据：** `db/session.py` 有 9 个补偿函数：
```
:163  _ensure_sqlite_account_app_key_index
:194  _ensure_sqlite_schedule_ownership_indexes
:218  _ensure_sqlite_publish_log_ownership
:278  _ensure_sqlite_multi_account_bindings
:363  _ensure_sqlite_product_key_scope
:484  _assert_sqlite_operator_emails_unique
:515  _ensure_sqlite_operator_email_index
:533  _ensure_sqlite_operator_username_index
:579  _backfill_sqlite_account_bindings
```
对应的 Alembic 迁移：
```
_ensure_sqlite_account_app_key_index      ←→ 20261030_0916
_ensure_sqlite_schedule_ownership_indexes ←→ 20261102_1003
_ensure_sqlite_publish_log_ownership      ←→ 20261102_1004
_ensure_sqlite_multi_account_bindings     ←→ 20261103_1009
_ensure_sqlite_product_key_scope          ←→ 20261103_1007
_ensure_sqlite_operator_email_index       ←→ 20261102_1002
_ensure_sqlite_operator_username_index    ←→ 20261029_2100
_backfill_sqlite_account_bindings         ←→ 20261102_1001 + 20261103_1009
（_assert_ 是校验，无迁移对应）
```
9 个函数 vs 8 条迁移，**没有任何文档或测试断言它们语义等价**。每对都是一份逻辑写两遍（Python 裸 SQL + Alembic op），这是典型的漂移温床 —— `20261114_0001` 丢失 `uq_operators_email_normalized` 就是「只有配对的一侧被修」的结果。

- **性质：补丁叠补丁。** 补偿函数是为了修「老库不走 Alembic」这个问题而加的补丁；加完之后就形成了第二套 schema 事实源。

- **影响：** 新增一条迁移时，很可能忘记同步补偿函数（`short_id` 就是反例：两边都没有）。

- **根因修复方案：** 建一张对照表放进 `docs/migrations.md`，并在 CI 里加测试：对每个补偿函数，断言「全新 create_all 库」与「跑完该补偿函数的老库」结构一致。

---

### A-P2-2 `docs/migrations.md` 的迁移清单漏了 6 条

**证据：**
```
$ ls server/migrations/versions/*.py | wc -l
26
$ grep -c "^| \`2026" docs/migrations.md
17
```
逐条比对（docs 清单 vs 实际 revision）：
```
docs 漏掉的：
  20261103_1007   scope_product_key_uniqueness
  20261103_1008   automation_image_lifecycle
  20261103_1009   multi_account_bindings
  20261103_1010   schedule_product_selection
  20261114_0001   multi_device_tokens
  20261119_0001   schedule_multi_shop_targets
```
`docs/migrations.md:100-120` 的表格停在第 20 行（`20261103_1006`）。而 `:174` 写着「CI 的迁移检查另行统计出 46 个显式索引」——**这个数字也过时了**（实测现在是 47/48，见 A-P1-1）。

- **性质：补丁叠补丁（文档债）。**

- **影响：** 新接手的人按文档理解迁移链会漏掉 6 个版本，其中包括 `20261103_1007`（唯一的 `warehouse_key` 唯一性作用域变更）和 `20261114_0001`（就是它弄丢了邮箱唯一索引）。

- **根因修复方案：** 把清单改成从 `ScriptDirectory` 自动生成，或加一条测试断言「docs 表格里的 revision 集合 == `versions/*.py` 的 revision 集合」。

---

### A-P2-3 `_SHORT_ID_CHARS` 与注释不符，且 8 位兜底超出 `String(8)` 语义

已在 A-P0-1 证据 8 展开。列在此处是因为它单独也构成「注释与实现不一致」的可维护性问题。

---

### A-P2-4 `active_query()` 的 `deleted_at IS NULL` 是软删除，但没有配套的清理/唯一约束处理

**证据：**
`services/warehouse_service.py:649-650`
```python
def active_query(db: Session):
    return db.query(Product).filter(Product.deleted_at.is_(None))
```
`models/product.py:128` 的唯一约束是 `UniqueConstraint("account_id", "detail_id", "shop_id")`，**不含 `deleted_at`**。所以：软删除一件商品后，同一 `(account_id, detail_id, shop_id)` **无法重新入仓**（会撞唯一约束）。

代码里因此走「软删除 + 复活」而不是删除重建。但没有任何地方清理过期的 `deleted_at` 行，`products` 表只增不减（**表级 soft delete 无保留策略**）。

- **性质：补丁叠补丁。** 唯一约束先定（不含 deleted_at），软删除后加，两者语义冲突，靠「永远不真删」绕过。

- **影响：** 表无限增长；若将来做真删除清理，会释放唯一键从而允许重复入仓。

- **根因修复方案：** 明确二选一——(a) 唯一约束改成部分索引 `WHERE deleted_at IS NULL`（推荐）；(b) 定期归档 + 真删。

---

## 迁移漂移清单

> 生成方式：`alembic upgrade head` vs `Base.metadata.create_all` 各建临时库，逐表/列/索引/唯一约束/外键比对 + `migrations/_verify_baseline.py` 交叉验证。

### 确认的漂移（2 处）

| 表 | models 定义 | 迁移累积 | 差异与后果 |
|---|---|---|---|
| `operators.short_id` | `String(8), unique=True`（`models/operator.py:57`） | **完全缺失**（26 个迁移零命中） | PG：`no such column` 直接崩；SQLite：列补上但唯一约束缺失，可插重复值。**P0** |
| `operators` 唯一索引 `uq_operators_email_normalized` | `Index(..., func.lower(func.trim(email)), unique=True, ...)`（`models/operator.py:66-68`） | `20261102_1002` 建了，`20261114_0001` 的 `batch_alter_table("operators")` 重建表时丢失 | PG 无邮箱唯一性（并发可注册同邮箱）；SQLite 靠 `_ensure_sqlite_operator_email_index()` 补救。**P1** |

### 默认值漂移（2 列，属同一模式）

| 表.列 | models 定义 | 迁移累积 | 差异与后果 |
|---|---|---|---|
| `schedules.completed` | `default=False`，**无** `server_default`（`models/schedule.py:52`） | `server_default=sa.false()`（`20261103_1010:17`） | create_all 库上原生 INSERT 报 `NOT NULL constraint failed`。**P1** |
| `schedules.completed_runs` | `default=0`，**无** `server_default`（`models/schedule.py:53`） | `server_default="0"`（`20261103_1010:18`） | 同上 |

### 结构等价（21 张表，无差异）

`account_bindings` `accounts` `audit_logs` `automation_run_items` `automation_runs` `batch_items` `batches` `categories` `email_verification_codes` `groups` `image_jobs` `login_rate_limits` `operator_tokens` `password_reset_tokens` `product_claims` `products` `publish_logs` `schedule_logs` `settings` `shops` `violation_words`

—— 列类型、可空性、主键、外键（含 ondelete）、具名索引全部一致。

### 迁移质量逐条

| 迁移 | 幂等 | downgrade | 破坏性操作 |
|---|---|---|---|
| `f6add52fca2e` baseline | 否（`create_table` 硬建） | ✅ 完整 | 无 |
| `e3079cb089af` audit_logs+operators | 否 | ✅ | 无 |
| `d4f39fe694e0` operator_id ×3 | 否（`add_column` 无 checkfirst） | ✅ | 无 |
| `20261029_2044` automation_result | 否 | ✅ | 无 |
| `20261029_2100` login credentials | 否 | ✅ | 无 |
| `20261030_0910` product_claims | 否 | ✅ | 无 |
| `20261030_0911` account_ownership | 否 | ✅ | 无 |
| `20261030_0912` automation_runs | 否 | ✅ | 无 |
| `20261030_0913` quality_review | 否 | ✅ | 无 |
| `20261030_0914` draft_verified_revision | 否 | ✅ | 无 |
| `20261030_0915` word_rule_provenance | 否 | ✅ | 无 |
| `20261030_0916` unique app keys | 否（有重复检查，安全中止） | ✅ | 无（检查后建索引） |
| `20261030_0917` login_rate_limits | 否 | ✅ | 无 |
| `20261102_1000` email+reset tokens | 否 | ✅ | 无 |
| `20261102_1001` account_bindings | ✅（`ON CONFLICT DO NOTHING`） | ✅（有不可表示检查） | 无 |
| `20261102_1002` email codes | 否（有重复邮箱检查） | ✅ | 无 |
| `20261102_1003` schedule ownership | 否 | ✅ | **降级丢数据**（删 owner 列）|
| `20261102_1004` publish_log ownership | 否 | ✅（有重复检查） | 否（重建表但复制全部数据）|
| `20261103_1005` email_verified | 否 | ✅ | **降级丢数据** |
| `20261103_1006` legacy fallback | 否 | ✅（拒绝降级）| 无 |
| `20261103_1007` product key scope | ✅（先探测再删） | ✅（有重复检查） | 否（重建表） |
| `20261103_1008` automation_images | 否 | ✅ | 无 |
| `20261103_1009` multi account bindings | ✅（`ON CONFLICT DO NOTHING`） | ✅（有重复检查） | 否（重建表） |
| `20261103_1010` schedule product selection | 否 | ✅ | 无 |
| `20261114_0001` multi_device tokens | 否 | ✅ | **⚠ 静默丢索引**（见 A-P1-1 证据 4） |
| `20261119_0001` schedule targets | 否 | ✅ | 无 |

**downgrade 全部真实实现**（无 `pass` 占位）—— 这一点做得比多数项目好。有三条降级会丢数据（`1003`/`1005`/`1000`），但都在 `docs/migrations.md:121-127` 明确记录。

**破坏性操作无备份**：`db/session.py` 里有 3 处「重建表」（`_ensure_sqlite_publish_log_ownership`、`_ensure_sqlite_multi_account_bindings`、`_ensure_sqlite_product_key_scope`），都是 `CREATE 新表 → INSERT SELECT → DROP 旧表 → RENAME`。这三处**在 `engine.begin()` 单事务内完成**（SQLite DDL 事务安全），且有「未知列先中止」保护，**设计是稳的**。但都没有先做 `.bak` 快照 —— 一旦事务中途进程被杀，SQLite 会回滚，所以实际风险低。

---

## CI 健康度实测（截至 2026-11-26）

| 检查项 | 命令 | 结果 |
|---|---|---|
| ruff | `python -m ruff check .` | **40 errors** → CI fail |
| pytest（CI 范围） | `python -m pytest tests -m unit -q` | **4 failed / 78** → CI fail |
| 迁移冒烟 | `python scripts/ci_migration_check.py` | exit 0（但把真实漂移降级成 warning）|
| 逐列漂移 | `python migrations/_verify_baseline.py` | **exit 1**（正确检出 short_id + schedules 默认值）—— 但**不在 CI 里** |
| pytest（全量） | `python -m pytest tests -q` | 覆盖面远超 CI，含鉴权矩阵/多租户隔离 |

---

## 建议的回归测试

每条格式：**测什么 → 怎么断言**。

### 针对 A-P0-1（short_id 漂移）

1. **`test_migrations_create_every_model_column`**
   在临时库跑 `alembic upgrade head`，对每个 `Base.metadata.tables` 里每张表用 `PRAGMA table_info` 取列集合，断言 `model_columns == alembic_columns`。
   → 断言消息必须逐列列出缺哪些（不要只报数量）。

2. **`test_operators_short_id_unique_index_exists`**
   在临时库跑 `alembic upgrade head` 后 `PRAGMA index_list('operators')`，断言存在一个 unique 索引，其 `PRAGMA index_info` 的列是 `['short_id']`。

3. **`test_legacy_db_upgrade_creates_short_id_uniqueness`**
   手搓一个无 `short_id` 的 `operators` 表（含 2 行）→ 跑 `init_db()` → 插入两条相同 `short_id='123456'` → 断言第二次抛 `IntegrityError`。
   → 这是唯一能抓住「`_add_missing_columns` 补列但补不上约束」的测试形态。

4. **`test_short_id_generator_respects_column_width`**
   连续生成 100 个 `_generate_short_id()`，断言每个 `len(sid) <= 8`。若实现改成 8 位兜底+查重，断言 100 个互不相同。

### 针对 A-P0-2（CI 门禁）

5. **`test_ci_runs_auth_and_tenant_tests`**
   断言 `pyproject.toml` 的 CI marker 集合覆盖 `test_role_permissions.py`、`test_account_ownership.py`、`test_shared_account_task_isolation.py` 这三个文件——用 `pytest --collect-only -m unit` 的文件列表断言包含它们。
   → 这条测试防止「CI 覆盖面被静默收窄」。

6. **`test_price_template_freight_matches_default_table`**
   不硬编码 0.18/0.42，改为从 `DEFAULT_TEMPLATE["freightTable"]` 派生期望值：
   ```python
   ft = DEFAULT_FREIGHT_TABLE
   assert cross_border_freight_myr("0.001", DEFAULT_TEMPLATE) == Decimal(str(ft["firstWeightPrice"]))
   ```
   → 这样改费率时测试自动跟上，不会再出现「代码改了测试没改」。

### 针对 A-P0-3（凭据泄漏）

7. **`test_no_debug_dumps_tracked`**
   CI 里跑 `git ls-files`，断言结果里不含匹配 `^\.(captcha_|diag\.|kw_|c[123]_|img|upload_|tun\.|login\.err)` 的路径。
   → 纯 shell 断言即可，零成本，永久生效。

### 针对 A-P1-1（索引被静默丢弃）

8. **`test_batch_alter_preserves_expression_indexes`**
   对 `operators` 表：先建 `uq_operators_email_normalized`，再跑一次 `op.batch_alter_table("operators")` 加任意列，断言索引仍存在。
   → 这条直接钉住 `20261114_0001` 的失败模式。

9. **`test_alembic_downgrade_then_upgrade_is_idempotent`**
   `upgrade head → downgrade base → upgrade head` 两次，断言两次 `upgrade head` 后的 schema 签名完全一致（用 `_verify_baseline.table_signature()` 复用即可）。

### 针对 A-P1-2（默认值漂移）

10. **`test_non_optional_columns_have_server_default`**
    遍历 `Base.metadata.tables`，对每个 `nullable=False` 且非主键的列，断言 `col.server_default is not None`。

11. **`test_raw_insert_works_on_create_all_db`**
    在 `init_db()` 建出的库上，对每张表用**只含主键**的原生 `INSERT`，断言不抛 `NOT NULL constraint failed`。

### 针对 A-P1-3（索引策略）

12. **`test_warehouse_list_query_has_no_full_table_scan`**
    在 2000 行 `products` 上跑 `EXPLAIN QUERY PLAN` 的仓库列表 SQL，断言输出不含裸 `SCAN products`（允许 `SCAN ... USING INDEX`）。
    → 加索引后此测试转绿，是修复的验收标准。

13. **`test_json_path_filters_are_declared`**
    对 `warehouse_service` 里每个 `json_extract`/`->>` 查询条件，断言对应字段有真实列或表达式索引（用一张 `{json_path: column_or_index}` 映射表断言，映射缺项即 fail）。
    → 防止把新的筛选字段再塞进 JSON。

### 针对 A-P1-4（weight 无列）

14. **`test_weight_is_a_queryable_column`**
    断言 `Product.__table__.c` 含 `weight_kg`，且 `CheckConstraint` 覆盖 `weight_kg > 0`。
15. **`test_weight_clamping_single_source_of_truth`**
    对同一输入（原始重量 + 品类），断言 `product_standard.resolve_weight()`、`publish_service` 的裁剪、`price_template` 的计费**三处取到同一个值**。
    → 这是抓「同一概念多处各算一遍」的唯一办法（`rules.md` 的 `test_every_copy_gets_its_own_link` 同款思路）。

### 针对 A-P1-6（事务边界）

16. **`test_service_layer_never_commits`**
    `grep -n "db.commit()" server/services/*.py`，断言只命中白名单文件（`claim_job_service.py` / `publish_job_service.py` / `automation_run_service.py` 的 `run()` 顶层），其余 0 命中。
    → 纯静态断言，防止契约再漂移。

17. **`test_claim_job_unknown_outcome_is_terminal`**
    注入一个「`publish()` 抛异常」的 fake，断言：`batch_items` 里该件状态是 `failed`、`batch.errors` 有对应消息、且**该件不会被再次投递**（`_run_plan` 不含它）。

### 针对 A-P2-1（补偿函数对照）

18. **`test_ensure_functions_match_migrations`**
    对每个 `_ensure_sqlite_*` 函数：建一个「缺该约束的老库」→ 调该函数 → 断言 schema 签名与「`alembic upgrade head` 建出的库」的对应部分一致。

### 针对 A-P2-2（文档漂移）

19. **`test_migrations_doc_lists_every_revision`**
    用 `ScriptDirectory.from_config()` 取所有 revision，与 `docs/migrations.md` 表格里解析出的 revision 集合做集合相等断言。

---

## 附：审计方法与可复现性

本报告全部结论可由下列命令复现（均不触碰真实 `app.db`，`AOHUANG_DATA_DIR` 指向 `mkdtemp`）：

```bash
cd server

# 1. 逐列漂移（正确检出）
python migrations/_verify_baseline.py          # exit 1 = 有漂移

# 2. CI 用的迁移冒烟（会放行漂移）
python scripts/ci_migration_check.py           # exit 0 = 但压掉了 warning

# 3. CI 两道门禁的真实状态
python -m ruff check .                         # 40 errors
python -m pytest tests -m unit -q              # 4 failed / 78

# 4. 全量测试（含鉴权/多租户，不在 CI 范围）
python -m pytest tests -q

# 5. 凭据泄漏
git ls-files | grep -E '^\.(captcha_|kw_|c[123]_|img|upload_)'
```
