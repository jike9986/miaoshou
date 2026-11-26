# 审计 C：API 路由与鉴权

> 审计范围：`server/api/`（26 文件 8 367 行，其中 24 个路由模块 + `deps.py` + `helpers.py` + `reference.py`）、`server/schemas/`（459 行）、`server/core/`（1 148 行：`config.py` 317 / `constants.py` 260 / `errors.py` 164 / `utils.py` 236 / `locks.py` 170）
> 旁证（只读引用）：`server/main.py`、`server/services/{operator,account,warehouse,publish,setting,share,group_access,automation_run,schedule}_service.py`、`server/tests/test_role_permissions.py`、`docs/architecture.md`
> 审计方式：**只读静态审计 + 活体探针**。探针用 `fastapi.testclient.TestClient` 起真实 `app`、指向 `tempfile.mkdtemp()` 临时数据目录，多身份（admin / teacher / member / 匿名 / 伪造 Bearer / 伪造 `X-Operator`）逐端点打。**未修改任何源码。**
> 端点总数：**187 个 method+path**（用 `app.routes` 遍历 FastAPI 依赖树得出，非 grep 装饰器文本），148 个唯一路径。
> 审计基准：`master` @ `4c9f32d`，版本 0.1.54。

---

## 结论摘要

**骨架是对的，细节在漏水。**

`_guarded` 这个设计（`api/__init__.py:50-53`，router 级挂 `require_token` + `current_operator` + `require_password_change_complete` 三道）是**教科书级的正确做法**：一次挂载、全子树继承，新加模块不可能忘记鉴权。**独立复核（Lead 遍历 `app.routes` 依赖树）：183 个 API 端点中 172 个强制鉴权，11 个无 token 的全部是有意公开的登录/注册/健康检查端点**（`/system/health`、`/system/share/health`、`/auth/login`、`/auth/email-code-login/*`、`/auth/register`、`/auth/registration-code/request`、`/auth/password-reset/*`、`/auth/credentials-status`、`/auth/initialize-credentials`）。这一层没有漏。这是我在这个仓库里见到的最有纪律的一层。

但纪律到此为止：

1. **`_guarded` 之外的家庭成员各自为政**。`operators.router` / `operators.audit_router` / `system.router` 绕开 `_guarded` 单独挂载（`api/__init__.py:72-73`、`:79-82`），前两者**丢了第三道 `require_password_change_complete`**，后者**把三道手抄了一遍**。抄写的东西会漂移——`system.router` 抄对了，`operators.router` 抄漏了一道。实测：一个 `must_change_password=True` 的老师可以 `POST /operators`（建号）、`PATCH /operators/{id}`（改名/停用）、`POST /operators/{id}/rotate`（抢 token），而同一个 token 打 `/warehouse` 是 403。**改密码这道门只挡住了老实人。**

2. **受限分组的闸门只装在列表页**。`group_access.guard_group`（`services/group_access.py:106`）**全仓零调用者**——写了、注释写得漂亮、没接上。`guard_products` 只接了 3 个地方（claim_job / publish_job / schedule），`publish_service.py` **一处都没有**。实测学员能 `GET /warehouse/{id}?includePayload=1` 读到「精品」组商品全文、能 `POST /warehouse/{id}/claim` 认领它。列表页看不见、直接按 id 打就能拿——这是**用前端隐藏当权限**的经典翻车。

3. **`shared_mode` 有两个真值源，而鉴权读的那个永远读不到界面写的那个**。`share_service.shared_mode(db)` 优先读 settings 表（`services/share_service.py:141-157`），`Settings.shared_mode` 只读环境变量（`core/config.py:84`，`get_settings()` 带 `@lru_cache`，`core/config.py:313`）。`require_token` 判断「共享部署不接受本机 secret」时读的是**后者**（`api/deps.py:86`、`:116`）。实测：界面上打开共享模式 → `/system/share` 报告 `bindHost: 0.0.0.0`、`sharedMode: true` → **同一个进程里 secret.key 打 `/operators` 返回 `role: admin`**；重启后（环境变量没设）`settings.shared_mode` 仍是 `False`，secret.key 照样是管理员。**共享部署 = 人人管理员**，只要部署方忘了写那个环境变量。而 `core/config.py:82` 的注释白纸黑字写着「它**不是**权限开关：没有任何鉴权逻辑读它」——注释与代码互相打脸。

4. **信息泄漏面全开在「只看身份、不看角色」的那 49 个端点上**。`GET /system/settings` 对任意 member 返回 `dataDir` + `dbPath`（`api/system.py:335-336`）；`GET /auth/status` 返回 `dataDir`（`api/auth.py:709`）；`GET /system/bootstrap` 返回 `dataDir`（`api/system.py:212`）；`GET /images/verify` 返回密钥文件绝对路径（`api/images.py:353`）；`GET /system/update` 返回上一个安装包的绝对路径（`api/system.py:116`）。**这些都不需要管理权限。**

5. **错误码契约已经破了**。`core/errors.py` 定义了 40 个常量，但代码里裸写字符串的错误码有 **15 个**没登记，其中 `"CONFLICT"` **36 处**、`"AUTH_FAILED"` **9 处**。前端按 code 分支的逻辑拿到的是一堆不在合同里的值。更硬的一个：`api/system.py:760` 用了 `errors.INTERNAL`——**这个常量不存在**（`errors.py:29` 只有 `INTERNAL_ERROR`），`/system/logs/tail` 一旦读文件失败就是 `AttributeError` → 兜底 500。

**P0 共 6 条，P1 共 9 条，P2 共 10 条。**

### 分级统计

| 级别 | 条数 | 越权 | 信息泄漏 | 一致性 / 健壮性 | 可维护性 |
|---|---|---|---|---|---|
| **P0** | 6 | 4 | 2 | 0 | 0 |
| **P1** | 9 | 1 | 1 | 6 | 1 |
| **P2** | 10 | 0 | 0 | 4 | 6 |
| **合计** | **25** | **5** | **3** | **10** | **7** |

### 性质分布

| 性质 | 条数 | 说明 |
|---|---|---|
| **根因** | 4 | 设计层面缺失，改一行补不上 |
| **补丁叠补丁** | 13 | 在旧错上叠新错，越补越绕 |
| **跷跷板** | 8 | 修了 A 就必须坏 B，两个真值源互搏 |

### 最严重的 3 条

1. **C-P0-1 `shared_mode` 双源分裂 → 共享部署下本机 secret.key 仍是管理员凭据**
   影响面：任何「多人共用一台服务器」的部署，只要没设 `AOHUANG_SHARED_MODE=1` 环境变量（而界面明确告诉运维「不用设，我落库了」），**每个能连上的客户端都持有管理员权限**。这是本报告唯一的「拿到就能全权控制」的问题。

2. **C-P0-2 受限分组闸门零接入 → 学员能读、能认领、能认领后发布「精品」组商品**
   影响面：负责人口径是「这三组拿货价是标高的，学员按它发出去是卖一件亏一件」。列表页挡住了，详情页和认领接口没挡。**亏钱是直接结果，不是风险。**

3. **C-P0-3 定价模板「选中项」是全局键 → 老师选的贵模板串到学员界面，且学员无权改回**
   影响面：模板决定售价。学员 B 打开模板页看到 `selectedName: "老师的贵模板"`、自己的模板列表是 `['马来','泰国','越南','菲律宾','新加坡']`（名字对不上），而 `POST /templates/select` 需要 `process` 权限 → **学员被锁在一个改不掉的错误选中项上**。按原价/错价上架是钱。

---

## P0 问题（越权 / 信息泄漏）

### C-P0-1 `shared_mode` 双源分裂：界面写 settings 表，鉴权读环境变量 —— 共享部署下本机 `secret.key` 仍是管理员凭据

- **证据 1**：`shared_mode` 是第一等配置项，且**注释明确声称没有鉴权逻辑读它**

`server/core/config.py:79-84`
```python
    # 共享服务器模式（多人共用同一份仓库）。
    # True 时后端监听 0.0.0.0，别人才能连上；单独开这个开关会在启动日志里
    # 打一条显眼的告示（见 main.py 的 shared_mode_warning）。
    # 注意它**不是**权限开关：没有任何鉴权逻辑读它，它只影响监听地址和
    # 自动更新（共享模式下不做自动安装，否则一个人升级会把所有人的服务重启）。
    shared_mode: bool = False
```
`server/api/deps.py:82-87`（`require_token` 内）
```python
    if _same_secret(supplied, settings.local_secret()):
        # 本机 secret 视部署模式而定：
        #  · 单机（默认，绑 127.0.0.1）：它就是这台机器的身份，直接放行，
        #    用户无需登录软件账号（产品负责人 2026-10-30 明确要求）。
        #  · 共享部署（多人共用一台服务器）：secret 会人手一份，若也放行等于
        #    人人管理员；必须拒绝，改用个人令牌。
        if settings.shared_mode:
```
`server/api/deps.py:114-121`（`_authenticate` 内，同样读 `settings.shared_mode`）

**注释说「没有任何鉴权逻辑读它」，而那两行代码就是鉴权逻辑。** 这是文档与实现的直接矛盾，也是后面所有麻烦的起点。

- **证据 2**：另一个「共享模式」真值源，优先级完全不同

`server/services/share_service.py:141-157`
```python
def shared_mode(db: Session | None = None) -> bool:
    """共享模式开关：settings 表 > 环境变量 AOHUANG_SHARED_MODE。

    为什么表优先：运维在界面上点过「共享」之后，重启时环境变量可能已经不在了
    （双击 exe 启动的那条路径不设变量），这时候必须还认库里那个开关，
    否则「重启一下又变回单机」——这正是共享部署最难查的那类问题。
    """
    if db is not None:
        try:
            from services import setting_service

            stored = setting_service.get(db, SHARED_MODE_KEY)
            if isinstance(stored, bool):
                return stored
        except Exception:  # noqa: BLE001 - 表读失败就退回环境变量
            pass
    return bool(getattr(get_settings(), "shared_mode", False))
```

- **证据 3**：界面开关写的是 settings 表，不是环境变量，也不是内存

`server/api/system.py:891-919` → `share_service.set_shared_mode(db, value)`；`server/services/share_service.py:523-527`
```python
def set_shared_mode(db: Session, value: bool) -> dict[str, Any]:
    """切换共享模式：落库 + 明确告知「要不要重启」。

    监听地址是 uvicorn 启动参数，进程活着就改不了 —— 所以这里不假装生效，
    直接回 `restartRequired: true`，并写清重启后该是什么地址。
    """
    from services import setting_service

    setting_service.put(db, SHARED_MODE_KEY, bool(value))
```
**只写表。`Settings.shared_mode` 一个字节都没动。** 而 `get_settings()` 带 `@lru_cache(maxsize=1)`（`core/config.py:313-317`），进程内连重新读环境变量都不会发生。

- **证据 4**：`listen_host` 读的是 `Settings.shared_mode`，`share_view` 报的是 `share_service.shared_mode`，两个函数不会同时为真

`server/core/config.py:170-185`
```python
    @property
    def listen_host(self) -> str:
        """后端实际监听地址。三个来源，优先级从高到低：
          1. `AOHUANG_BIND_HOST` —— 部署时想绑某个具体网卡（如 192.168.1.5）；
          2. `AOHUANG_SHARED_MODE=1` —— 多人共用，绑所有网卡；
          3. 默认 —— 只绑回环，别人连不上，等于「每人在自己机器上装一份」。
        ...
        explicit = (self.bind_host or "").strip()
        if explicit:
            return explicit
        return "0.0.0.0" if self.shared_mode else "127.0.0.1"
```
`server/main.py:50` `HOST = BOOT_SETTINGS.listen_host`，`:627` `uvicorn.run(app, host=host, port=port, ...)` —— uvicorn 绑的是 `Settings` 那一路。

- **实证输出 1（同一进程内打开共享模式，secret.key 立刻放行）**

`AOHUANG_BIND_HOST=0.0.0.0`、不设 `AOHUANG_SHARED_MODE`，起 TestClient：
```
进程视角 settings.shared_mode = False
进程视角 listen_host        = 0.0.0.0
secret.key 前 8 位         = jR8q6Iw9

=== 1. 用 secret.key 当业务凭据（单机模式放行）===
200 {"items":[],"page":1,"pageSize":20,"total":0,...}

=== 2. 在界面上把共享模式打开（只写 settings 表）===
set_shared_mode -> {'sharedMode': True, 'bindHost': '0.0.0.0', 'restartRequired': False, 'previousBindHost': '0.0.0.0'}
DB 视角 shared_mode = True
进程视角 settings.shared_mode = False （未重启，仍是旧值）

=== 3. 没有重启，secret.key 还能不能当业务凭据？===
200 {"items":[],...}                       ← /warehouse 放行
GET /operators with secret -> 200 admin    ← 角色解析成 admin

=== 4. 而 /system/share 自己报告的 sharedMode ===
200 sharedMode = True  bindHost = 0.0.0.0  restartRequired = False
```
注意 `restartRequired: False` —— 因为 `before_host == after_host`（`0.0.0.0` → `0.0.0.0`），界面会告诉运维「不用重启，已经生效了」。**而鉴权那边根本没生效。**

- **实证输出 2（重启后依然如此：库说共享，进程说单机）**

进程 A 把 `sharedMode=True` 写入 settings 表；进程 B 全新启动、环境变量都不设：
```
重启后（环境变量未设 AOHUANG_SHARED_MODE / AOHUANG_BIND_HOST）：
  settings.shared_mode      = False   ← uvicorn 实际用这个
  settings.listen_host      = 127.0.0.1
  share_service.shared_mode = True   ← 界面/接口用这个
  share_view.bindHost       = 0.0.0.0
```
三个值互相矛盾：进程绑 `127.0.0.1`（真实），接口报 `bindHost: 0.0.0.0`（假的），鉴权按单机放行 `secret.key`（放水）。**重启也救不回来，因为写表的那个开关永远不会被 `Settings` 读到。**

- **性质**：**跷跷板**（两个真值源互搏）+ **补丁叠补丁**（`share_service.shared_mode` 是为了解决「重启后丢配置」这个真问题而加的第二真值源，但只补了「显示」一侧，没补「鉴权」一侧；`core/config.py:82` 那句「没有任何鉴权逻辑读它」是更早的一层补丁遗留的过时注释）。

- **影响**：
  - 任何共享部署，只要没显式设 `AOHUANG_SHARED_MODE=1`（而界面明确暗示不用设），**持有 `secret.key` 的人就是管理员**。`secret.key` 落在 `data_dir/secret.key`（`core/config.py:155`），Electron 侧把它注入给 sidecar，共享服务器上它会随部署目录/备份/截图扩散。
  - `require_token` 的拒绝分支（`deps.py:86`）在「界面开启共享」这条主路径上**永远不会触发**。
  - 反方向也坏：如果运维设了 `AOHUANG_SHARED_MODE=1` 但**没**在界面点开关，`listen_host` 与 `share_view` 也会短暂不一致（`share_service.shared_mode` 表里没值才回落环境变量，这条路径侥幸对）。
  - `restartRequired: False` 给运维的错误信号是这场事故的放大器——它让人以为改完了。

- **根因修复方案**：
  1. **单一真值源**：删掉 `Settings.shared_mode` 这个字段，或者把它降级成「启动时的一次性快照」，并让 `share_service.shared_mode(db)` 成为唯一入口。`require_token` / `_authenticate` / `listen_host` 全部改调它。
  2. **启动时把库里的值灌回进程**：`lifespan` 里（`init_db()` 之后、`uvicorn` 开始接受请求之前做不到，因为 `listen_host` 是 `main.py:50` 模块级算的）——正确做法是把 `shared_mode` 的判断**移出启动参数决策**：`listen_host` 只认 `bind_host` / 显式 `AOHUANG_SHARED_MODE`（部署参数），而**鉴权**只认库里的值。两者本来就该分开：一个是「绑哪个网卡」，一个是「这台机器是不是多人共用」。
  3. **鉴权必须 fail-closed**：`require_token` 遇到「库说共享 / 环境说单机」这个矛盾态时，应当**拒绝** `secret.key`（宁可让单机用户多点一次登录，也不能让共享部署人人管理员）。目前是 fail-open。
  4. **删掉 `core/config.py:82` 那句注释**，或改成「它被 `api/deps.py:86/116` 读取，见 C-P0-1」。

- **回归测试怎么写**：
  ```python
  def test_shared_mode_from_settings_table_disables_local_secret(tmp_path, monkeypatch):
      """库里的 sharedMode=True 必须让 secret.key 不再是业务凭据。"""
      monkeypatch.setenv("AOHUANG_DATA_DIR", str(tmp_path))
      monkeypatch.delenv("AOHUANG_SHARED_MODE", raising=False)
      init_db()
      db = SessionLocal()
      share_service.set_shared_mode(db, True)   # 只写库，模拟界面操作
      db.commit(); db.close()

      client = TestClient(app, raise_server_exceptions=False)
      secret = get_settings().local_secret()
      for path in ("/api/v1/warehouse", "/api/v1/operators", "/api/v1/system/settings"):
          r = client.get(path, headers={"Authorization": f"Bearer {secret}"})
          assert r.status_code == 401, f"共享模式下 {path} 仍接受本机 secret：{r.status_code}"
          assert r.json()["error"]["code"] == "TOKEN_INVALID"

  def test_shared_mode_view_and_auth_agree(tmp_path, monkeypatch):
      """share_view 报的 sharedMode 必须与 require_token 的判定同源。"""
      monkeypatch.setenv("AOHUANG_DATA_DIR", str(tmp_path))
      monkeypatch.delenv("AOHUANG_SHARED_MODE", raising=False)
      init_db(); db = SessionLocal()
      share_service.set_shared_mode(db, True); db.commit()
      view = share_service.share_view(db)
      secret_ok = _secret_accepted_by_require_token(db)   # 抽一个可测的谓词
      assert view["sharedMode"] is True and secret_ok is False, \
          f"界面说共享({view['sharedMode']})，鉴权说单机({secret_ok}) —— 双源分裂"
      db.close()

  def test_shared_mode_env_and_table_conflict_fails_closed(monkeypatch, tmp_path):
      """环境=单机、库=共享 的矛盾态必须拒绝 secret.key（fail-closed）。"""
      monkeypatch.setenv("AOHUANG_DATA_DIR", str(tmp_path))
      monkeypatch.delenv("AOHUANG_SHARED_MODE", raising=False)
      init_db(); db = SessionLocal()
      share_service.set_shared_mode(db, True); db.commit(); db.close()
      client = TestClient(app, raise_server_exceptions=False)
      r = client.get("/api/v1/warehouse",
                     headers={"Authorization": f"Bearer {get_settings().local_secret()}"})
      assert r.status_code == 401
  ```
  再加一条**结构测试**：grep 全仓，要求 `settings.shared_mode` 这个属性**没有**读取点（或只允许一处，且有注释说明为什么）。

---

### C-P0-2 受限分组（自营海外仓 / 海外仓定制款 / 精品）的闸门零接入：列表页挡住了，详情页 / 认领 / 发布全通

- **证据 1**：闸门写好了，`guard_group` **全仓零调用者**

`server/services/group_access.py:106-121`
```python
def guard_group(db: Session, operator: Operator | None, group_id: Any, *,
                action: str = "使用") -> None:
    """写入/发布/排任务前的闸门：学员碰受限分组直接 403。

    这是**接口层**的拦截，不能只靠前端藏起来 ——
    绕过前端直接调 API 是最常见的越权路径。
    """
    if not is_restricted_group(db, group_id):
        return
    if may_access_restricted(operator):
        return
    raise errors.AppError(
        errors.FORBIDDEN,
        f"这个分组是受限精品组，学员不能{action}，请联系老师处理",
        status=403, details={"groupId": str(group_id),
                            "groups": list(RESTRICTED_GROUP_NAMES)})
```
`grep -rn "guard_group(" --include=*.py .` 输出（排除定义处）：
```
=== guard_group 被谁调用 ===
(以上为非定义处调用)
```
**空。** 注释里说「接口层的拦截」，而接口层一次都没调用它。

- **证据 2**：`guard_products` 只接了 3 条路，`publish_service.py` 零接入

```
=== guard_products 调用点 ===
./services/claim_job_service.py:133
./services/publish_job_service.py:184
./services/schedule_service.py:610
```
`grep -c "group_access" services/publish_service.py` → **`0`**。而 `POST /api/v1/publish`（`api/publish.py:125`）走的正是 `publish_service.publish`。对比之下 claim_job / publish_job / schedule 三条路都有闸门——说明闸门是**逐个补**上去的，补了三处，漏了主路径。

- **证据 3**：列表页的 SQL 过滤是真正的（唯一生效的那道），而详情 / 认领不经过它

`server/services/warehouse_service.py:785-790`（`list_products` 内，唯一生效的过滤）
```python
    from services import group_access

    visible = group_access.visible_group_ids(db, viewer)
    if visible:
        query = query.filter(or_(Product.group_id.is_(None),
                                 Product.group_id.notin_(visible)))
```
`server/services/warehouse_service.py:1378-1382`（`detail`，学员可直接按 id 打）
```python
def detail(db: Session, ident: Any, *, include_payload: bool = False,
           operator_id: int | None = None, claim_account_id: Any = None) -> dict[str, Any]:
    product = resolve_product(db, ident)
    _assert_member_product_access(db, product, operator_id)
```
`server/services/warehouse_service.py:1446-1448`（`claim_product`）
```python
def claim_product(db: Session, ident: Any, owner_operator_id: int) -> ProductClaim:
    product = resolve_product(db, ident)
    _assert_member_product_access(db, product, owner_operator_id)
```
**两处都没有 `group_access` 调用。** `_assert_member_product_access`（`:412-431`）只看「是不是别人的自动化结果」，不看分组。

- **实证输出（学员 member 身份，商品 group_id=1 = `自营海外仓`/`精品`）**
```
group id = 1 product id = 1

=== 学员 GET /warehouse（列表应看不到）===
200 total = 0 groups facet = []          ← 列表页正确挡住了

=== 学员 GET /warehouse/{pid}（详情）===
200 {"product":{"id":"1","key":"D1:S1","detailId":"D1","shopId":"S1","title":"受限精品样品",...
                                          ← 按 id 直接打，全文拿到

=== 学员 PATCH /warehouse/{pid} groupId（改分组）===
403 FORBIDDEN process                ← 正确（靠 process 权限顺带挡住）

=== 学员 POST /warehouse/{pid}/claim（认领受限商品）===
200 {"id":"1","productId":"1","ownerOperatorId":"2","status":"claimed",...}
                                          ← 认领成功
```
第二组（group_id=1 = `精品`，带 `includePayload`）：
```
=== 学员认领「精品」组商品 ===
200 {"id":"1","productId":"1","ownerOperatorId":"1","status":"claimed",...}
=== 学员 GET /warehouse/{pid} 拿到 payload（详情泄漏）===
200 title = 精品样品 groupId = 1 有 payload? True
=== 学员 GET /warehouse?groupId={gid}（显式筛受限组）===
200 total = 0                        ← 列表口径仍然正确
=== 学员 GET /groups（分组列表）===
200 {"groups":[],"items":[],"total":0}   ← 分组名也不露（正确的补丁）
```
**同一份数据，两个入口两个结果。** 列表页 `total=0`（看不见），详情页 `200`（看得见）。这就是「前端隐藏当权限」。

- **证据 4**：`is_restricted_group_id`（按 id 兜底的免查库版本）只在图片截断处用了一次，说明「需要快速判断」的场景里，作者知道该拦、只是没铺开

`server/services/warehouse_service.py:3485-3490`
```python
    from services import group_access, pre_save_check
    ...
    if sku_limit and group_access.is_restricted_group_id(group_id):
```

- **性质**：**根因**（权限模型缺一层「按 id 取单件也要过一次分组判定」的统一入口）。逐个 `if` 往上补是 13 处 `group_access` 引用的由来，补了三处（claim_job / publish_job / schedule），漏了 detail / claim / publish —— 这部分是**补丁叠补丁**。

- **影响**：
  - 负责人口径（`warehouse_service.py:780-783` 注释）：「这三组的成品走合作厂家、不走采购，拿货价是标高的，学员按它发出去看起来就是卖一件亏一件（实测这三组 18 件 100% 售价 < 拿货价）」。现在学员可以：列表页看不到 → 拿 id 打开详情（拿到 SKU / 拿货价 / payload）→ 认领 → 用 `POST /publish` 发到自己的店铺（`publish_service.py` 零 group guard）。**整条链路是通的，唯一挡住的是「你得知道 id」。** 而 id 是连续整数，`GET /warehouse/{1..N}` 遍历即可。
  - 详情返回里含 `payload`（完整商品数据，含图片 URL、SKU、成本），不只是标题。
  - 这条不是「理论风险」：`group_access.py:1-30` 的记录说明这是 2026-11-19 负责人专门指认的 18 件商品。

- **根因修复方案**：
  1. **统一入口**：把分组判定塞进 `resolve_product(db, ident, *, operator_id=None)` —— 目前 `resolve_product`（`warehouse_service.py:653`）已经有 `account_id` 参数但**没有任何调用方传**，就是同一类「留了口子没接」的模式。给 `resolve_product` 加 `operator` 参数，内部调 `group_access.guard_group`。所有按 id 取商品的路由（`GET /{ident}`、`POST /{ident}/claim`、`POST /{ident}/translate-colors`、`PATCH /{ident}`、`DELETE /{ident}`、`PATCH /{ident}/images`、`PATCH /{ident}/quality-review`）自动继承。
  2. **`publish_service.publish` 补 `guard_products`**：`entries` 里有 `entry["product"].group_id`，一行循环即可，位置放在 `_resolve_items` 之后、任何上游调用之前（照抄 `claim_job_service.py:126-134` 的写法）。
  3. **删掉零调用的 `guard_group`，或立刻接上**。留着比删掉更危险——它让 `grep group_access` 的读者以为已经防住了。
  4. `visible_group_ids` 的 SQL 过滤保留（列表 + facets 口径正确），但**不能是唯一一道**。

- **回归测试怎么写**：
  ```python
  @pytest.mark.parametrize("group_name", ["自营海外仓", "海外仓定制款", "精品"])
  def test_member_cannot_reach_restricted_product_by_id(client, tokens, group_name):
      """列表看不见 != 看不见：按 id 直接打的每条路都必须 403。"""
      pid = _make_product(group_name=group_name)          # 造一件受限组商品
      h = auth(tokens, "member")
      probes = [
          ("GET",   f"/api/v1/warehouse/{pid}",              None),
          ("GET",   f"/api/v1/warehouse/{pid}?includePayload=1", None),
          ("POST",  f"/api/v1/warehouse/{pid}/claim",        None),
          ("POST",  f"/api/v1/warehouse/{pid}/translate-colors", {"language": "ms"}),
          ("PATCH", f"/api/v1/warehouse/{pid}",              {"title": "x"}),
          ("DELETE",f"/api/v1/warehouse/{pid}",              None),
      ]
      for method, path, body in probes:
          r = client.request(method, path, json=body, headers=h)
          assert r.status_code == 403, f"{method} {path} 学员能打（{r.status_code}）"
          assert r.json()["error"]["details"].get("groupId") == str(gid), \
              "403 要说明是受限分组，而不是笼统 forbidden"

  def test_member_cannot_publish_restricted_product(client, tokens):
      """发布主路径必须也过 guard（publish_service 目前零 group_access）。"""
      pid = _make_product(group_name="精品")
      acc = _make_account_bound_to_member(tokens)
      setting_service.set_publish_enabled(db, True)
      r = client.post("/api/v1/publish", headers=auth(tokens, "member"),
                      json={"accountId": str(acc.id), "shopId": "S1",
                            "confirm": True, "items": [{"warehouseId": str(pid)}]})
      assert r.status_code == 403
      assert "受限" in r.json()["error"]["message"]

  def test_teacher_still_sees_restricted_products(client, tokens):
      """闸门不能误伤老师（否则修完这版老师也用不了精品组）。"""
      pid = _make_product(group_name="精品")
      assert client.get(f"/api/v1/warehouse/{pid}",
                        headers=auth(tokens, "teacher")).status_code == 200

  def test_no_dead_group_guard():
      """guard_group 若有定义就必须至少有一处非测试调用。"""
      src = _all_source_text()
      calls = len(re.findall(r"guard_group\(", src)) - 1   # 减掉定义
      assert calls > 0, "guard_group 定义了但零调用 —— 权限闸门是装饰品"
  ```

---

### C-P0-3 定价模板「选中项」存在全局键里：老师选的贵模板串到学员界面，且学员无权改回

- **证据 1**：模板库是每人一套，**选中项不是**

`server/services/setting_service.py:239-264`（模板库，按 operator 分键 —— 正确）
```python
def price_templates(db: Session,
                    operator_id: int | None = None) -> dict[str, Any]:
    """这个操作员自己那套定价模板。

    每人一套，**没有共享模板**（负责人 2026-11-23 明确）。第一次读的时候
    如果他还没有任何模板，就用内置默认值起一套并落库 —— 界面上不该是空表，
    那会让人以为功能坏了。
    ...
    """
    key = _templates_key(operator_id)
```
`server/services/setting_service.py:136-140`
```python
def _templates_key(operator_id: int | None = None) -> str:
    """模板库的存储键。每人一套，operator_id 为空时用共享键（仅历史数据/离线用）。"""
    if operator_id is None:
        return PRICING_TEMPLATES_KEY
    return f"{PRICING_TEMPLATES_KEY}.operator.{int(operator_id)}"
```

`server/services/setting_service.py:261-274`（选中项，**全局键，只按 account 字符串分**）
```python
def selected_templates(db: Session) -> dict[str, str]:
    value = get(db, PRICING_SELECTED_KEY) or {}
    accounts = value.get("accounts") if isinstance(value, dict) else None
    return {str(k): str(v) for k, v in (accounts or {}).items()}


def select_template(db: Session, name: str, account: str = "") -> str:
    current = get(db, PRICING_SELECTED_KEY) or {}
    if not isinstance(current, dict):
        current = {}
    accounts = current.get("accounts") if isinstance(current.get("accounts"), dict) else {}
    accounts[str(account or "")] = str(name or "")
    put(db, PRICING_SELECTED_KEY, {**current, "accounts": accounts})
    return str(name or "")
```
**注意签名：`select_template(db, name, account)` —— 没有 `operator_id`。** 而 `price_templates` / `save_template` / `delete_template` 全都有。这是「每人一套」改造时漏掉的那个函数。

- **证据 2**：读接口把全局 `selected` 直接吐给所有人（只对 member 做了「账号可见性」过滤，没做「操作员可见性」过滤）

`server/api/system.py:452-477`
```python
@router.get("/templates", summary="定价模板列表")
def templates(db: DbSession, operator: Operator | None = Depends(current_operator),
              accountId: str | None = None) -> dict[str, Any]:
    ...
    stored = setting_service.price_templates(db, operator_id=_template_scope(db, operator))
    ...
    account = _member_account_scope(db, operator, accountId)
    selected = setting_service.selected_templates(db)
    if (isinstance(operator, Operator)
            and operator_service.role_of(operator) == operator_service.ROLE_MEMBER):
        owned_ids = {row["id"] for row in account_service.account_summaries(db)}
        selected = {key: value for key, value in selected.items()
                    if key == "" or key in owned_ids}
```
`owned_ids` 过滤的是**账号**不是**操作员**。同学员共享同一个妙手账号时（文档明确说「一个妙手账号可以由多个学员共享」），选中项就是共享的——而它指向的模板名只有老师那套里有。

- **证据 3**：学员想改回来，`POST /templates/select` 要 `process` 权限，学员没有

`server/api/system.py:656-659`
```python
@router.post("/templates/select", summary="记住某个账号当前用的是哪套定价模板",
             dependencies=[require_permission("process")])
def select_template(payload: dict[str, Any], db: DbSession,
                    operator: OperatorDep) -> dict[str, Any]:
```
`server/services/operator_service.py:74-83`
```python
ROLE_PERMISSIONS: dict[str, set[str]] = {
    ROLE_ADMIN: {"*"},
    ROLE_TEACHER: {"*"},
    ROLE_MEMBER: {"warehouse", "words", "product_claim", "publish_claimed",
                  "workflow.settings",
                  "account.bind", "schedule", "automation"},
}
```
`process` 不在 member 集合里 → 403。

- **实证输出**（老师 A 与学员 B 共享同一个妙手账号 `aid`；A 有一套叫「老师的贵模板」、利润率 60%，把它选给 `aid`）
```
settings 表里的 pricing.selected = {'accounts': {'1': '老师的贵模板'}}
老师A selected_template_name = 老师的贵模板
学员B selected_template_name =            ← 解析结果为空（B 库里没这个名字）

=== 老师 A GET /system/templates ===
200 selected = {'1': '老师的贵模板'} selectedName = 老师的贵模板
=== 学员 B GET /system/templates ===
200 selected = {'1': '老师的贵模板'} selectedName =            ← 空
    B 自己的模板 = ['马来', '泰国', '越南', '菲律宾', '新加坡']

=== 学员 B POST /system/templates/select（想改回自己的）===
403 {"error":{"code":"FORBIDDEN","message":"当前角色（member）没有「process」权限，请联系老师",...}}
```
读到的 `selected` 指向一个 B 根本看不见的模板名（`'老师的贵模板'`），`selectedName` 是空串（四级兜底 `selected_template_name` 查 B 的库找不到这个名字就跳过），而 B 想选自己的那套 → 403。**界面处于「有个选中项、但指向不存在的东西、且改不了」的死锁态。**

- **证据 4**：这条链路的钱在哪里——选中模板直接决定售价

`server/services/setting_service.py:371-402`
```python
def selected_template_name(db: Session, account_id: str | None = None,
                           operator_id: int | None = None) -> str:
    """按四级兜底取当前选中模板名，取不到返回 ""。

    1. 该账号显式选中的（pricing.selected.accounts[accountId]）
    2. 全局选中的（pricing.selected.accounts[""]）
    3. 妙手侧记录的（settings["pricing.miaoshouSelected"]）
    4. 模板库里唯一那一套（只有 1 套时自动用它）

    为什么必须四级：少一级，用户每次发布、每次建定时任务都得重选模板，
    选漏就按原价上架 —— 这是钱的 bug，不是体验问题。
    """
```
`server/services/publish_service.py:1187-1189`（发布时兜底取模板）
```python
    if member_claimed_publish and not str(template_name or "").strip():
        template_name = setting_service.selected_template_name(
            db, str(account.id), operator_id=operator_id)
```
四级兜底的**第 2 级 `accounts[""]` 是跨操作员共享的**：任何一个老师保存了 `accounts[""]`（全局选中），所有学员的兜底都会命中它。

- **性质**：**跷跷板**（「每人一套模板」这个正确改造只改了模板库，没改选中项；改一边另一边就漏）+ **补丁叠补丁**（`selected_templates` 上那层 `owned_ids` 过滤是发现泄漏后加的补丁，补的是账号维度，漏了操作员维度）。

- **影响**：
  - 学员的发布走四级兜底的第 2 级，可能落到**老师的模板**上——老师的模板往往利润率不同（实证里是 60% vs 默认 20%），按 60% 利润率算出来的售价是「卖不出去」的价格；反过来学员的模板被老师用上则是「亏本卖」。降级情况下落到第 4 级「库里唯一那一套」时同理。
  - 学员**无法自救**（改选中项要 `process`）。
  - `selected` 字段还会经 `GET /system/templates` 和 `GET /system/bootstrap`（`api/system.py:191`）两条路暴露给前端，前端按它显示「当前模板」，显示的是一个不属于自己的名字。

- **根因修复方案**：
  1. `select_template` / `selected_templates` 加 `operator_id` 参数，键改成 `pricing.selected.operator.{id}.accounts`，与 `_templates_key` 同构。迁移时把老全局键读出来按「当前唯一操作员」灌入（或直接丢弃，因为老数据本来就没按人分）。
  2. `POST /system/templates/select` 的权限从 `process` 降到 `workflow.settings`（member 已有），因为它是**改自己的偏好**、不是**改共享数据**。同时保留 `process` 之外的必要校验：`name` 必须在自己那套库里（`api/system.py:672-673` 已有这个校验，只是它用的是 `_template_scope`，改完要跟着走）。
  3. `publish_service` 取模板时**禁止**回落到 `accounts[""]` 这一级——跨用户共享的默认值是隐患。要么删掉第 2 级，要么让第 2 级也带 operator 前缀。

- **回归测试怎么写**：
  ```python
  def test_template_selection_is_per_operator(client, tokens, db):
      """A 选了模板，B 的 selected 里不能出现它（同名账号也不行）。"""
      aid = _shared_account_bound_to(tokens, ["teacher", "member"])
      setting_service.save_template(db, {"name": "老师的贵模板",
                                         "site": "MY", "profitPercent": 60.0},
                                    operator_id=_op_id(tokens, "teacher"))
      setting_service.select_template(db, "老师的贵模板", account=str(aid),
                                      operator_id=_op_id(tokens, "teacher"))
      db.commit()
      t = client.get("/api/v1/system/templates", headers=auth(tokens, "teacher")).json()
      m = client.get("/api/v1/system/templates", headers=auth(tokens, "member")).json()
      assert t["selected"].get(str(aid)) == "老师的贵模板"
      assert m["selected"].get(str(aid)) in (None, ""), \
          f"学员看到了别人的选中项：{m['selected']}"
      assert m["selectedName"] == ""

  def test_member_can_change_own_template_selection(client, tokens):
      """学员改自己的模板选中项不能被 process 权限挡住。"""
      aid = _shared_account_bound_to(tokens, ["member"])
      own = _first_template_name(client, tokens, "member")
      r = client.post("/api/v1/system/templates/select",
                      headers=auth(tokens, "member"),
                      json={"name": own, "account": str(aid)})
      assert r.status_code == 200, f"学员改自己偏好被拒：{r.status_code} {r.text}"
      assert r.json()["selected"][str(aid)] == own

  def test_publish_never_borrows_another_operators_template(client, tokens, db):
      """发布兜底不能命中别人的模板（四级兜底第 2 级是跨用户的）。"""
      setting_service.select_template(db, "老师的贵模板", account="",
                                      operator_id=_op_id(tokens, "teacher"))
      db.commit()
      name = setting_service.selected_template_name(db, "999",
                                                    operator_id=_op_id(tokens, "member"))
      assert name != "老师的贵模板"
  ```

---

### C-P0-4 任何登录成员都能读到 `dataDir` / `dbPath` / 密钥文件路径 / 安装包路径 —— 5 个端点、零角色要求

- **证据 1**：`GET /system/settings` 只要求「是个人」，不要求角色

`server/api/system.py:329-343`
```python
@router.get("/settings", summary="只读模式、发布开关与当前账号处理参数")
def system_settings(db: DbSession, operator: OperatorDep) -> dict[str, Any]:
    settings = get_settings()
    return {
        "readOnly": setting_service.is_read_only(db),
        "publishEnabled": setting_service.publish_enabled(db),
        "dataDir": str(settings.data_dir),
        "dbPath": str(settings.db_path),
        "imageKeyConfigured": bool(settings.image_api_key()),
        "sharedMode": bool(settings.shared_mode),
        ...
```
`OperatorDep`（`api/deps.py:220`）只解析身份，不判角色。

- **证据 2**：`GET /auth/status` 同样泄漏 `dataDir`

`server/api/auth.py:694-712`
```python
@router.get("/status", summary="当前连接状态")
def status(db: DbSession) -> dict[str, Any]:
    ...
    return {
        "connected": account is not None,
        ...
        "serverVersion": APP_VERSION,
        "serverName": SERVER_NAME,
        "dataDir": str(settings.data_dir),
```
注意它的签名里**连 `operator` 都没有**（靠 `_guarded` 继承的 `current_operator` 兜底）。

- **证据 3**：`GET /system/bootstrap` 也带 `dataDir`

`server/api/system.py:210-213`
```python
        "sharedMode": bool(settings.shared_mode),
        "dataDir": str(settings.data_dir),
        "imageKeyConfigured": bool(settings.image_api_key()),
        "languages": list(SUPPORTED_LANGUAGES),
```

- **证据 4**：`GET /images/verify` 给出密钥文件的绝对路径与端点

`server/api/images.py:341-357`
```python
    if not key:
        return {
            "configured": False,
            "mode": "",
            "keyHint": "",
            "source": "",
            "path": "",
            "endpoint": settings.edits_endpoint(),
            "message": _missing_key_message(mode),
        }
    ...
    return {
        "configured": True,
        "mode": mode,
        "keyHint": key_hint,
        "source": source,
        "path": str(origin.get("path") or ""),
        "endpoint": settings.edits_endpoint(),
```
（`keyHint` 对 member 已做裁剪，`path` 没有 —— `api/images.py:345-347` 只裁了 `keyHint`。）

- **证据 5**：`GET /system/update` 给出上一个安装包的绝对路径

`server/api/system.py:108-117`
```python
        "downloaded": False,
        "canInstall": update_service.can_install(),
        "readOnly": setting_service.is_read_only(db),
        # 顺带告诉界面上次下的包还在不在，省得重复下一遍几十兆
        "pendingFile": last,
        "checkedAt": payload.get("checkedAt"),
```
`last = update_service.last_downloaded()`（`services/update_check.py:242-253`）返回 `str(max(candidates, key=lambda p: p.stat().st_mtime))` —— 绝对路径。

- **证据 6**：`GET /system/share` 把 `lanAddresses` + 在线客户端 + `dbPath` 打包给所有登录用户

`server/services/share_service.py:435-471`
```python
def share_view(db: Session | None = None) -> dict[str, Any]:
    ...
    return {
        "sharedMode": shared,
        "bindHost": host,
        "port": port,
        "portRange": [...],
        "lanAddresses": addresses,
        "clientCount": len(operators),
        "activeOperators": operators,
        "dbPath": facts["dbPath"],
        "dbEngine": facts["dbEngine"],
        "dbSizeBytes": facts["dbSizeBytes"],
        ...
        "shareUrls": [f"http://{address}:{port}" for address in addresses],
```

- **实证输出（member 身份，逐端点）**
```
=== 4. member GET /system/settings ===
200 {"readOnly": false, "publishEnabled": false,
     "dataDir": "C:\\Users\\Administrator\\AppData\\Local\\Temp\\auditc_aus25clw",
     "dbPath":  "C:\\Users\\Administrator\\AppData\\Local\\Temp\\auditc_aus25clw\\app.db",
     "imageKeyConfigured": false, "sharedMode": false, ...}

=== 5. member GET /auth/status ===
200 dataDir= C:\Users\Administrator\AppData\Local\Temp\auditc_aus25clw
    serverVersion= 0.1.54
    keys= ['accountId','accounts','appIdHint','connected','connectionName',
           'dataDir','languages','limits','publishEnabled','readOnly',
           'serverName','serverVersion']

=== 6. member GET /system/share ===
200 {"sharedMode": false, "bindHost": "127.0.0.1", "port": 8812,
     "portRange": [8812, 8822],
     "lanAddresses": ["192.168.1.43", "192.168.2.100", "192.168.133.2"],
     "clientCount": 1,
     "activeOperators": [{"clientId": "testclient", "ip": "testclient", ...}],
     "dbPath": "C:\\Users\\Administrator\\AppData\\Local\\Temp\\auditc_aus25clw\\app.db",
     "dbEngine": "sqlite", "dbSizeBytes": ...}

=== 8. member GET /images/verify ===
200 {"configured": false, "mode": "", "keyHint": "", "source": "", "path": "",
     "endpoint": "https://image.codesonline.dev/v1/images/edits",
     "message": "图片翻译密钥未配置。……① 把密钥写入
       C:\\Users\\Administrator\\AppData\\Local\\Temp\\auditc_aus25clw\\image-key.txt
       （一行纯文本，UTF-8）；② 设置环境变量 AOHUANG_IMAGE_KEY 后重启；
       ③ 沿用老版位置 C:\\Users\\Administrator\\AppData\\Local\\tk-product-automation\\..."}
```

> **证据校核（与 audit-a-data 的口径差异）**：`audit-a-data` 报告 member 打 `/system/settings`、`/system/templates`、`/groups`、`/warehouse` 得到 **403**。我这边同样四个端点得到 **200**。差异根因是**测试账号的 `must_change_password` 标志不同**——`_guarded` 的第三道依赖 `require_password_change_complete`（`api/deps.py:223-233`）在那个标志为真时对所有业务端点返 403 `PASSWORD_CHANGE_REQUIRED`，与角色无关。我实测过这条路径：
> ```
> 学员（must_change_password=True）：
> GET /api/v1/operators       -> 200      ← 在 _guarded 外，绕过了这道门
> GET /api/v1/system/settings -> 403 PASSWORD_CHANGE_REQUIRED
> GET /api/v1/system/share    -> 403 PASSWORD_CHANGE_REQUIRED
> ```
> 所以那边观察到的 403 **不是**角色隔离生效的证据，而是「密码未改」这个副作用；去掉这个标志后 200。这条差异本身直接指向 **C-P1-1**。

- **性质**：**根因**（错误响应里混进了「部署细节」这一类本不该出现在业务 API 里的东西）+ **补丁叠补丁**（`/images/verify` 对 `keyHint` 做了成员裁剪、`/share/health` 对路径做了裁剪——两处裁剪证明作者知道这是泄漏面，但每次只裁当前被投诉的那个字段）。

- **影响**：
  - `dbPath` / `dataDir` 告诉攻击者「SQLite 文件在哪」，配合任何文件读/写类问题（本仓有 `logs/tail` 读文件、`update` 落盘、`size-charts/upload` 落盘）都能定向。
  - `lanAddresses` + `shareUrls` 在共享部署下直接给出「内网可达地址清单」。
  - `activeOperators` 给出在线人数与 `clientId`/`ip`（`share_service.py:419-431`），可用来做「什么时候有人在线」的时序判断。
  - `pendingFile` 暴露运维机器的目录结构（含 Windows 用户名）。
  - 这些都是**低权限账号即可拿**，而共享部署里低权限账号恰恰是最不可信的那批（学员）。

- **根因修复方案**：
  1. **在响应模型层面禁止**：给这几个端点定义明确的 Pydantic 响应模型（`schemas/`），字段白名单化。现在它们全部返回裸 `dict[str, Any]`，所以「多吐一个字段」永远不会有编译期/测试期阻力——这是泄漏反复发生的结构原因。
  2. **部署细节归管理角色**：`dataDir` / `dbPath` / `pendingFile` / `lanAddresses` / `dbSizeBytes` 只对 `admin`/`teacher` 返回；member 拿到的响应里这些键**直接不存在**（不是空串）。
  3. **`/images/verify` 的 `path`** 与 `keyHint` 同样对待（现在只裁了 `keyHint`）。
  4. **统一一个 `public_view` / `admin_view` 双视图函数**，别在每个端点里手写「这个字段给不给」——本仓已经有 `share_service.health_view`（`services/share_service.py:497-520`）这个正确样板，推广它。

- **回归测试怎么写**：
  ```python
  DEPLOY_DETAIL_KEYS = {"dataDir", "dbPath", "pendingFile", "lanAddresses",
                        "shareUrls", "dbSizeBytes", "bindHost", "path", "endpoint"}

  @pytest.mark.parametrize("path", [
      "/api/v1/system/settings", "/api/v1/system/bootstrap",
      "/api/v1/auth/status", "/api/v1/system/share", "/api/v1/images/verify",
      "/api/v1/system/update",
  ])
  def test_member_never_sees_deployment_paths(client, tokens, path):
      """成员响应里不允许出现任何绝对路径 / 内网地址字段。"""
      r = client.get(path, headers=auth(tokens, "member"))
      assert r.status_code in (200, 204), r.text
      body = r.text
      leaked = [k for k in DEPLOY_DETAIL_KEYS if f'"{k}"' in body]
      assert not leaked, f"{path} 给成员吐了部署细节：{leaked}"
      assert "AppData" not in body and "\\\\" not in body.replace("\\\\n", ""), \
          f"{path} 响应里出现 Windows 绝对路径"

  @pytest.mark.parametrize("path", ["/api/v1/system/settings", "/api/v1/auth/status"])
  def test_teacher_still_gets_paths(client, tokens, path):
      """裁剪不能误伤老师（否则运维排查不了）。"""
      r = client.get(path, headers=auth(tokens, "teacher"))
      assert r.status_code == 200
      assert "dataDir" in r.text
  ```

---

### C-P0-5 免鉴权的两个 health 端点在全网卡部署下对整网段暴露：版本号、在线人数、监听地址、数据库可写状态

- **证据 1**：`/system/health` 免鉴权，返回 `version` + `uptimeSeconds`

`server/api/system.py:63-66`
```python
@health_router.get("/health", summary="健康检查（无鉴权，供 Electron 探活）")
def health(db: DbSession) -> dict[str, Any]:
    state = system_service.db_state(db)
    return system_service.health(db_ok=state == "ok", db_state=state)
```
`server/services/system_service.py:32-39`
```python
def health(db_ok: bool = True, db_state: str = "ok") -> dict[str, Any]:
    return {
        "ok": bool(db_ok),
        "name": "aohuang-listing-server",
        "version": APP_VERSION,
        "db": db_state,
        "uptimeSeconds": round(time.time() - _STARTED_AT, 1),
    }
```

- **证据 2**：`/system/share/health` 免鉴权，返回 `serverVersion` + `operatorCount` + `bindHost` + `sharedMode`

`server/api/system.py:857-871`
```python
@health_router.get("/share/health", summary="共享服务端探活（无鉴权，客户端拿 token 前用）")
def share_health(request: Request) -> dict[str, Any]:
    """免鉴权探活：客户端还没拿到 token 时就要先问「服务器活着吗」。
    ...
    返回里刻意**没有** dbPath / dataDir：这是整个网段都能打的地址，
    把服务器目录结构暴露出去没有任何好处。
    """
    from services import share_service

    share_service.note_client(request, authenticated=False)
    return share_service.health_view()
```
`server/services/share_service.py:497-520`
```python
def health_view(db: Session | None = None) -> dict[str, Any]:
    """`GET /system/share/health` —— 客户端**没有 token 时**也要能调。

    只回答「服务器活着、库能写、几个人在连」，不含任何路径与凭据细节：
    这是免鉴权接口，泄露 dbPath 等于把服务器目录结构告诉整个网段。
    """
    ...
    return {
        "ok": bool(facts.get("dbWritable")) and not facts.get("dbBusy"),
        "serverVersion": APP_VERSION,
        "uptimeSeconds": base.get("uptimeSeconds"),
        "dbWritable": bool(facts.get("dbWritable")),
        "dbBusy": bool(facts.get("dbBusy")),
        "operatorCount": len(operators),
        "sharedMode": shared,
        "bindHost": effective_bind_host(shared),
        "port": effective_port(),
        "name": base.get("name"),
    }
```
**注释自己承认了「这是整个网段都能打的地址」，但只裁掉了路径，留着版本号、在线人数、监听地址、库可写状态。**

- **证据 3**：后端确实会绑到全网卡

`server/core/config.py:170-185`（`listen_host`：`AOHUANG_BIND_HOST` > `AOHUANG_SHARED_MODE=1` > `127.0.0.1`）；`server/main.py:50` `HOST = BOOT_SETTINGS.listen_host`，`:627` `uvicorn.run(app, host=host, port=port, ...)`。`docs/architecture.md:63` 仍写「后端只监听 `127.0.0.1`」——过时，见 C-P2-8。

- **证据 4**：`GET /` 也免鉴权（FastAPI 默认，无任何依赖），返回 `version`

`server/main.py:558-567`
```python
@app.get("/", include_in_schema=False)
def root() -> dict[str, Any]:
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "api": API_PREFIX,
        "health": "/api/system/health",
        "docs": "/api/docs" if get_settings().dev else None,
    }
```

- **实证输出（匿名，无任何头）**
```
=== 1. 免鉴权 GET /api/v1/system/share/health ===
200 {"ok": true, "serverVersion": "0.1.54", "uptimeSeconds": 1.5,
     "dbWritable": true, "dbBusy": false, "operatorCount": 1,
     "sharedMode": false, "bindHost": "127.0.0.1", "port": 8812,
     "name": "aohuang-listing-server"}

=== 2. 免鉴权 GET /api/v1/system/health ===
200 {"ok": true, "name": "aohuang-listing-server", "version": "0.1.54",
     "db": "ok", "uptimeSeconds": 1.5}

=== 3. 免鉴权 GET / ===
200 {"name": "奥皇上品", "version": "0.1.54", "api": "/api/v1",
     "health": "/api/system/health", "docs": null}
```

- **性质**：**跷跷板**（探活必须免鉴权 ↔ 免鉴权就必然暴露信息；作者用「裁字段」这个手段在两难之间走钢丝，裁一半）+ **补丁叠补丁**（`health_view` 相对 `share_view` 少给字段，是发现泄漏后另开一个函数而不是加视图层）。

- **影响**：
  - **版本号指纹**：`0.1.54` + 公开的更新清单地址（`/system/version` 返回 `manifestUrl: https://aohuang888.com/miaoshou-downloads/version.json`）让外部能精确判断「这个部署落后几个版本」，再对着 changelog 找已修的漏洞。这不是理论：本仓 `docs/audit/` 里其他几份报告已经记录了多个版本修掉的安全问题。
  - **`operatorCount`**：共享部署下暴露「当前几个人在用」，可做作息规律推断。
  - **`bindHost` / `port`**：直接确认「我可以打这个端口」。
  - **`dbWritable` / `dbBusy`**：暴露「什么时候能写入成功」，对基于竞态的攻击有用。

- **根因修复方案**：
  1. **探活只回布尔**：`/system/health` 与 `/system/share/health` 在**未鉴权**时只返回 `{"ok": true}`；带 `Authorization` 且通过鉴权时才返回完整字段。实现上用一个 `require_token` 的**可选**版本（`optional_operator` 已有，`api/deps.py:257-290`）判断「要不要给详情」。
  2. **版本号移到已鉴权端点**：客户端需要版本号来判「要不要升级」，但那个判断可以在**登录后**做（登录是必走的一步）。`/system/version`（`api/system.py:69`）已经在 `_guarded` 里，够了。
  3. **`GET /` 删掉 `version`**，或整条路由 `include_in_schema=False` 且只在 dev 下注册。
  4. 若因 Electron 探活时序无法去掉版本号，退一步：只暴露「主版本.次版本」（`0.1`），不暴露 patch（`54`）——patch 号才是「有没有修这个洞」的信号。

- **回归测试怎么写**：
  ```python
  @pytest.mark.parametrize("path", ["/api/v1/system/health",
                                    "/api/v1/system/share/health",
                                    "/api/system/health",
                                    "/"])
  def test_anonymous_health_leaks_no_fingerprint(client, path):
      """免鉴权探活只能回答「活着」，不能回答「我是什么版本、几个人在用」。"""
      r = client.get(path)
      assert r.status_code in (200, 404), r.text
      if r.status_code == 404:
          return
      body = r.text
      for forbidden in ('"version"', '"serverVersion"', '"operatorCount"',
                        '"bindHost"', '"uptimeSeconds"', '"dbWritable"',
                        '"port"'):
          assert forbidden not in body, f"{path} 匿名可见 {forbidden}：{body[:200]}"

  def test_health_with_token_may_include_details(client, tokens):
      """已鉴权调用方仍要能拿到版本号（Electron 需要它判升级）。"""
      r = client.get("/api/v1/system/health", headers=auth(tokens, "member"))
      assert r.status_code == 200 and "version" in r.text
  ```

---

### C-P0-6 跨租户发布：`POST /publish` 的 `items[].warehouseId` 不校验商品归属，学员 A 的私有商品可用学员 B 的账号发出去

- **证据 1**：`_resolve_items` 按 id 直接取商品，不带任何操作员上下文

`server/services/publish_service.py:524-550`
```python
def _resolve_items(db: Session, raw: Iterable[Mapping[str, Any]]
                   ) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    missing: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise errors.validation(f"第 {index + 1} 项不是对象", field=f"items[{index}]")
        warehouse_id = item.get("warehouseId")
        detail_id = item.get("detailId")
        shop_id = item.get("shopId")
        if warehouse_id not in (None, ""):
            try:
                product = warehouse_service.resolve_product(db, warehouse_id)
            except errors.AppError as exc:
                if exc.code == errors.NOT_FOUND:
                    missing.append(str(warehouse_id))
                    continue
                raise
            resolved.append({
                "product": product,
                ...
```
`warehouse_service.resolve_product(db, warehouse_id)` —— **不传 `operator_id`，也不传 `account_id`**。对比 `warehouse_service.resolve_product` 的签名（`warehouse_service.py:653-657`）：
```python
def resolve_product(db: Session, ident: Any, *, account_id: int | None = None) -> Product:
    """按数字 id 或 `{detailId}:{shopId}` 取商品；软删除视为不存在。
    ...
```
那个 `account_id` 参数**全仓无调用方**（`grep -n "resolve_product(" services/` 只有不带它的调用）。

- **证据 2**：`detail_id + shop_id` 分支更直接——裸 ORM 查询，连 `resolve_product` 都不走

`server/services/publish_service.py:555-562`
```python
        text = str(detail_id).strip()
        if not text.isdigit():
            raise errors.validation(f"第 {index + 1} 项的 detailId 必须是数字",
                                    field=f"items[{index}].detailId", value=text)
        key = product_key(text, shop_id)
        product = (db.query(Product)
                   .filter(Product.warehouse_key == key, Product.deleted_at.is_(None))
                   .one_or_none())
```
无 `account_id`、无 `operator_id`、无 `group_access`。

- **证据 3**：发布路径的唯一「学员约束」是自查一下「是不是我自己的自动化结果」，不是「能不能碰这件商品」

`server/services/publish_service.py:1150-1163`
```python
    if member_publish:
        # 负责人 2026-11-05 明确：发布就是要上架，不需要先认领。
        # 学员可以直接把成品发到店铺（后端会自动建载体），
        # 所以这里不再要求「必须先认领」或「必须是本人自动化结果」。
        # 保留的唯一约束：必须是仓库里的成品（warehouseId），
        # 否则拿不到 payload，也就没法建载体。
        automation_ids = {entry["product"].id for entry in entries
                          if isinstance(entry.get("product"), Product)
                          and entry["product"].automation_result
                          and entry["product"].operator_id == operator.id
                          and entry["product"].account_id == account.id}
        member_automation_publish = bool(automation_ids)
```
注释说得很清楚：「不再要求必须先认领或必须是本人自动化结果」。**这个放宽本身是产品决定**（负责人 2026-11-05），但它把「商品归属」这个校验也一起放掉了——`member_automation_publish` 只影响**能不能跳过模板校验**，不构成访问控制。

- **证据 4**：`publish_service.py` 里 `group_access` 出现 0 次（见 C-P0-2 证据 2）

- **实证输出**（学员 B 用自己私有账号 `accB_id` 发布学员 A 私有账号下的「精品」组商品 `pid`）
```
学员 B 试图操作 学员 A 私有账号(accA) 的「精品」组商品 pid = 1

1) 读详情: 200                          ← C-P0-2 的入口
2) 认领  : 200                          ← C-P0-2 的入口
3) 发布到 B 自己的账号 → 403 PUBLISH_DISABLED   （因为发布总开关关着，与权限无关）
```
打开发布总开关后重跑：
```
B 对 A 私有账号的「精品」组商品 pid = 1
发布 → 409 {"error":{"code":"ACCOUNT_SECRET_UNREADABLE",
          "message":"账号「B私有账号」的凭据无法解密：……请重新填写 AppSecret"}}

B 用 A 的 accountId 直接发布：
→ 403 {"error":{"code":"FORBIDDEN","message":"无权访问其他学员的妙手账号"}}
```
**结论分两半**：
- 「B 用 A 的 accountId」被正确挡了（`account_service.resolve_account` → `_assert_account_access`，`services/account_service.py:46-51`）。
- 「B 用 B 自己的 accountId + A 的商品」**通过了全部归属校验**，卡住它的是「临时库里的 AppSecret 解密不了」这个环境问题，不是权限。在真实部署里 AppSecret 是能解的，这条请求会一路走到上游。**这是一条只在测试环境被偶然挡住的路。**

- **性质**：**根因**（`resolve_product` 从设计上就没有「操作员/账号上下文」这个参数，所有按 id 取商品的地方共享同一个洞）+ **补丁叠补丁**（`resolve_product` 上加了个 `account_id` 形参但零调用 —— 形状上已经承认了问题，没接线）。

- **影响**：
  - 学员 B 可以把学员 A 私有账号下、A 自己还没发布过的商品（含自动化处理结果）**用自己的店铺发出去**。商品归属记录（`publish_logs.operator_id`）会记成 B，货也从 B 的店铺出——**A 的处理成本被 B 收割**，且 A 完全不知情。
  - 配合 C-P0-2 可以发「精品」组的高拿货价商品（亏钱）。
  - 商品 id 是连续整数，`items: [{"warehouseId": "1"}, ..., {"warehouseId": "N"}]` 一次最多 `PUBLISH_MAX_ITEMS = 200` 件。

- **根因修复方案**：
  1. `_resolve_items(db, raw, *, operator, account)` 加两个必填参数；`warehouseId` 分支调 `warehouse_service.resolve_product(db, wid, account_id=account.id, operator=operator)`；`detailId+shopId` 分支的裸 query 也加上 `Product.account_id.in_(visible_account_ids)` 过滤。
  2. `resolve_product` 的 `account_id` 形参改成**必填**（或至少让 `services/` 内所有调用点都传），并把 `operator` 也加进去顺带做 `group_access.guard_group`（与 C-P0-2 的修复合并成一次改造）。
  3. `member_publish` 分支补一条：`entry["product"].account_id == account.id`（即「来源账号 == 目标账号」，或来源账号属于本人）——把产品决定（不要求先认领）与安全边界（不能碰别人账号的商品）解耦。
  4. 这条改动会连带 `POST /claim-jobs`、`POST /publish-jobs`（两者都走 `_resolve_items` 的同类逻辑），一起改。

- **回归测试怎么写**：
  ```python
  def test_member_cannot_publish_other_members_product(client, tokens, db):
      """B 用自己的账号发 A 私有账号的商品 —— 必须 403。"""
      accA, pidA = _product_owned_by(tokens, "member_b")   # A 的私有账号 + A 的商品
      accB = _account_bound_to(tokens, "member_c")          # B 自己的账号
      setting_service.set_publish_enabled(db, True)
      _give_readable_secret(accB)                           # 让凭据可解，绕过环境干扰
      r = client.post("/api/v1/publish", headers=auth(tokens, "member_c"),
                      json={"accountId": str(accB.id), "shopId": "SHOPB",
                            "confirm": True,
                            "items": [{"warehouseId": str(pidA)}]})
      assert r.status_code == 403, f"跨租户发布没被拦：{r.status_code} {r.text}"
      assert r.json()["error"]["code"] == "FORBIDDEN"

  def test_member_cannot_publish_by_detail_id_key(client, tokens, db):
      """detailId+shopId 分支是裸 ORM 查询，必须也带账号过滤。"""
      accA, pidA = _product_owned_by(tokens, "member_b")
      prod = db.get(Product, pidA)
      accB = _account_bound_to(tokens, "member_c"); _give_readable_secret(accB)
      setting_service.set_publish_enabled(db, True)
      r = client.post("/api/v1/publish", headers=auth(tokens, "member_c"),
                      json={"accountId": str(accB.id), "shopId": "SHOPB",
                            "confirm": True,
                            "items": [{"detailId": prod.detail_id,
                                       "shopId": prod.shop_id}]})
      assert r.status_code == 403

  def test_teacher_can_still_publish_any_product(client, tokens, db):
      """老师是全局角色，不能被上面两道锁误伤。"""
      accA, pidA = _product_owned_by(tokens, "member_b")
      setting_service.set_publish_enabled(db, True)
      r = client.post("/api/v1/publish", headers=auth(tokens, "teacher"),
                      json={"accountId": str(accA.id), "shopId": "SHOPA",
                            "confirm": True, "items": [{"warehouseId": str(pidA)}]})
      assert r.status_code not in (403, 401), r.text

  def test_resolve_product_requires_account_context():
      """结构性断言：resolve_product 的 account_id 形参必须有调用方传。"""
      src = _all_source_text()
      calls = re.findall(r"resolve_product\(([^)]*)\)", src)
      assert any("account_id=" in c for c in calls), \
          "resolve_product 的 account_id 参数仍然零调用 —— 跨租户洞未闭环"
  ```

---

---

## 附：Lead 独立复核记录（2026-11-26）

本报告的 6 条 P0 由 Lead 用独立脚本复跑验证，结论如下。

### 验证脚本

| 脚本 | 验证对象 | 用法 |
|---|---|---|
| `_verify_c_p0_1.py` | C-P0-1 shared_mode 双源分裂 | `cd server && python -X utf8 ../docs/audit/_verify_c_p0_1.py` |
| `_verify_c_p0_2.py` | C-P0-2 受限分组闸门零接入 | `cd server && python -X utf8 ../docs/audit/_verify_c_p0_2.py` |

### C-P0-1：确认成立，并精确到「两源分叉的那一行」

实测（临时库，显式不设 `AOHUANG_SHARED_MODE`）：

```
=== 初始状态 ===
  settings.shared_mode（环境变量源，鉴权读它）: False
  share_service.shared_mode(db)（表源）      : False

=== 模拟运维在界面上打开共享模式 ===
  settings.shared_mode（环境变量源）: False     ← 没变
  share_service.shared_mode(db)（表源）: True   ← 变了

一致: False    ★ 双源分裂成立
```

AST 定位鉴权读取点，**全部是环境变量源，没有一处调 share_service**：

```
deps.py:88   if settings.shared_mode:
deps.py:116  if settings.shared_mode:
```

`core/config.py:82` 的注释原文：

```
# 注意它**不是**权限开关：没有任何鉴权逻辑读它，它只影响监听地址和
```

> 注释说「没有任何鉴权逻辑读它」，实测有 **2 处**鉴权读它。**注释与代码互相打脸，且打脸方向是最危险的那种** —— 读注释的人会以为这里没有权限风险。

### C-P0-2：确认成立，用 AST 而不是 grep（排除注释里提到的假阳性）

```
=== guard_group 的 AST 调用点（排除定义本身）===
  调用点数量: 0
  ★ 零调用 —— 写了闸门但没接上

=== guard_products 的 AST 调用点 ===
  调用点数量: 3
    server/services/claim_job_service.py:133
    server/services/publish_job_service.py:184
    server/services/schedule_service.py:610

=== publish_service.py 里有没有分组闸门 ===
  提到 group_access : False
  调用任一分组闸门  : False
  ★ 同步发布路径完全没有分组闸门

=== 详情 / 认领端点是否有分组闸门 ===
  warehouse_detail     行  450  含分组闸门: False
  claim_product        行  466  含分组闸门: False
```

`group_access.guard_group` 的 docstring 写的是：

```python
def guard_group(db, operator, group_id, *, ...):
    """写入/发布/排任务前的闸门：学员碰受限分组直接 403。
```

**注释承诺「写入/发布/排任务前」，实际零调用。** 这是「写了正确的东西但没接上」的最典型样本 —— 比完全没写更危险，因为代码审查时看到函数存在就会以为已经防住了。

### C-P0-6：确认成立，并把「零调用」精确到数字

Lead 的统计（AST 精确匹配，排除测试文件）：

```
resolve_product 调用点总数（server/，排除 tests/）：23
其中显式传 account_id 的：0
唯一命中 account_id 的那行：server/services/warehouse_service.py:653（函数定义本身）
```

**关键对照**：`account_id` 这个参数名在全仓其它地方被**显式传了 381 次**。
所以这不是「本项目不习惯传账号上下文」，而是 `resolve_product` 这一处的**孤立遗漏**。

### 建议：把 C 报告末尾的结构性断言真的写进测试

C 报告已经给出了正确的测试方案（`test_resolve_product_requires_account_context`，用正则扫源码断言 `account_id=` 存在）。Lead 补充一点：
**建议用 AST 而不是正则**，因为 `resolve_product(db, ident, account_id=...)` 的正则容易被注释、字符串、跨行调用骗过。可直接复用 `_verify_c_p0_2.py` 里的 `scan_calls()` 实现。

---

## 附二：C-P0-2 修复方案的可执行性验证（Lead 实测）

审计给出了「把 `guard_group` 接到 4 个地方」的方案。但**接线前必须先确认两件事**，
否则接上去就是把正常业务打死：

```bash
cd server && python -X utf8 ../docs/audit/_verify_c_p0_2_fix.py
```

### 好消息：guard_group 本身的角色矩阵是正确的

```
=== may_access_restricted：角色矩阵 ===
  member   可访问受限分组: False
  teacher  可访问受限分组: True
  admin    可访问受限分组: True

=== guard_group 对受限组的实际行为 ===
  member   → 拦住 (403 FORBIDDEN)   ✓
  teacher  → 放行                    ✓
  admin    → 放行                    ✓

=== 非受限组（id=5，避开 id 兜底集合）===
  member   普通组 → 放行 ✓
  teacher  普通组 → 放行 ✓
  admin    普通组 → 放行 ✓
```

**结论：`guard_group` 不需要改，只需要「接上」。** 这是最低风险的一类修复。

### 但发现两个必须一起处理的风险

#### 风险 1：`operator=None` 会被拦住 → 会打死定时任务

```
=== operator 为 None（内部调用/定时任务）时的行为 ===
  None → 拦住 (FORBIDDEN)
```

`guard_group` 的 `may_access_restricted(None)` 返回 False，所以 `None` 被当成「无权」。
而**定时任务 / 后台线程 / 认领任务**这些内部路径很可能不传 operator。
直接把闸门接进 `publish_service` 或 `schedule_service`，会让这些路径全部 403。

> 接线前必须先确认：那 4 个调用点的 `operator` 参数是真的有值，
> 而不是 `None`（或者显式接受「None 时放行，因为内部调用已经过了鉴权」）。

#### 风险 2：`RESTRICTED_GROUP_IDS = {1,2,3}` 的 id 兜底会误判新库

`is_restricted_group` 的实现是「名字匹配 **或** id 在 {1,2,3} 里」。
生产库里 1/2/3 确实是那三个精品组，所以没问题。

但**新部署 / 测试库从头建**时，自增 id 从 1 开始：
```
  新插入的普通组 id=1 → is_restricted_group = True（误判成精品组！）
  新插入的普通组 id=2 → True（误判）
  新插入的普通组 id=3 → True（误判）
  新插入的普通组 id=4 → False（正常）
```

后果：新环境里前三个普通组会被当成受限组，学员全部看不到。
这不是理论风险 —— 本报告第一条验证脚本就踩到了（我造的「普通款」拿到 id=1，被拦）。

> 建议：id 兜底只在「名字匹配不到、但库里确实存在这三个组」时启用；
> 或者干脆去掉 id 兜底，只认名字（名字是业务概念，id 是实现细节）。

### 修复清单（已验证可执行）

| 位置 | 改动 |
|---|---|
| `api/warehouse.py:450` `warehouse_detail` | 加 `guard_group(db, operator, 商品.groupId)` |
| `api/warehouse.py:466` `claim_product` | 同上 |
| `services/publish_service.py:536` | 加 `guard_products(db, operator, [warehouseId])` |
| `services/publish_service.py:560` | 同上（detailId 分支） |
| `services/group_access.py` | 修 id 兜底误判（风险 2） |
| 4 个调用点 | 确认 operator 不为 None（风险 2 的前置） |
