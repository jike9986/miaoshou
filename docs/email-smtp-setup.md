# 服务端邮件投递配置

邮件验证码由 FastAPI 服务端通过 SMTP 发送。配置项使用 `AOHUANG_` 环境变量前缀；SMTP 主机和发件地址必填，用户名与密码需同时配置或同时留空。

## 当前服务器接入记录（2026-11-03）

已核实 aohuang888.com 原网站使用 **Resend**。妙手服务复用该服务商配置，写入自己的 `/etc/aohuang-listing-v2.env`，不要求客户端填写邮箱服务地址，也不改动原网站服务。

| 项目 | 已核实状态 |
| --- | --- |
| SMTP | `smtp.resend.com:465`，隐式 SSL/TLS；`USE_SSL=true`、`USE_TLS=false` |
| 发信域 | `mail.aohuang888.com`；具体发件地址使用服务器现有配置 |
| 凭据保存 | 仅保存在妙手服务受限环境文件，不提交 Git，不写进桌面端 |
| 连接与认证 | 已通过真实 SMTP 连接及登录检查，该检查不发送邮件 |
| 本轮新版代码部署 | 已部署 `20261103-121800`，PostgreSQL 结构版本 `20261103_1007`；原数据数量保持，HTTPS 健康检查 200 |
| 用户邮箱实收 | `241****106@qq.com` 已分别实际收到注册、登录、找回密码三封验证码邮件 |
| 生产账号流程 | 注册 201、验证码登录 200、密码重设 200、新密码登录 200；注册账号为 `member`，邮箱已验证 |
| 桌面界面验收 | 实际客户端密码登录、账号展示、错误旧密码保会话、改密弹窗键盘行为及清缓存通过；首次设密恢复通过组件行为测试和独立审查，详见本轮验收记录 |

下面的说明对应当前仓库实现；“SMTP 登录成功”“验证码请求受理”“收件箱收到邮件”“账号流程完成”是四种不同的验收结果。

| 环境变量 | 用途 | 默认值 |
| --- | --- | --- |
| `AOHUANG_MAIL_HOST` | SMTP 主机名 | 空（未配置） |
| `AOHUANG_MAIL_PORT` | SMTP 端口 | `587` |
| `AOHUANG_MAIL_USERNAME` | SMTP 登录用户名；不要求认证时留空 | 空 |
| `AOHUANG_MAIL_PASSWORD` | SMTP 登录密码或应用专用密码；不要求认证时留空 | 空 |
| `AOHUANG_MAIL_FROM` | 发件邮箱地址 | 空（未配置） |
| `AOHUANG_MAIL_REPLY_TO` | 可选的回复地址 | 空 |
| `AOHUANG_MAIL_USE_TLS` | 使用 STARTTLS | `true` |
| `AOHUANG_MAIL_USE_SSL` | 使用隐式 SSL/TLS | `false` |
| `AOHUANG_MAIL_TIMEOUT` | SMTP 连接超时秒数，范围为 0 到 300 秒之间且不含 0 | `15` |

STARTTLS 和隐式 SSL/TLS 不能同时启用；配置用户名/密码时必须启用其中一种。主机缺失或发件地址缺失属于未配置；邮箱、端口、超时和认证字段组合也会校验。常见配置如下；将尖括号中的占位项替换为邮件服务商提供的信息，不要把密码提交到代码仓库。

**587 / STARTTLS**

```ini
AOHUANG_MAIL_HOST=<SMTP主机名>
AOHUANG_MAIL_PORT=587
AOHUANG_MAIL_USERNAME=<SMTP登录用户名>
AOHUANG_MAIL_PASSWORD=<SMTP应用专用密码>
AOHUANG_MAIL_FROM=<发件邮箱地址>
AOHUANG_MAIL_USE_TLS=true
AOHUANG_MAIL_USE_SSL=false
```

**465 / SSL/TLS**

```ini
AOHUANG_MAIL_HOST=<SMTP主机名>
AOHUANG_MAIL_PORT=465
AOHUANG_MAIL_USERNAME=<SMTP登录用户名>
AOHUANG_MAIL_PASSWORD=<SMTP应用专用密码>
AOHUANG_MAIL_FROM=<发件邮箱地址>
AOHUANG_MAIL_USE_TLS=false
AOHUANG_MAIL_USE_SSL=true
```

把变量放在服务管理器的受限环境文件中，不要放进源码、桌面客户端、命令行参数或日志。生产环境应限制环境文件权限，只允许管理员修改、服务进程读取；检查配置时不要打印或复制密码值。服务异常日志只记录异常类型，不记录邮件地址、SMTP 响应或认证凭据。

## 三条用户流程

所有路径均以 `/api/v1/auth` 为前缀，验证码为 6 位数字、15 分钟有效，按规范化邮箱和用途区分；同一用途重发会替换旧验证码。数据库只存带服务端密钥的 HMAC 摘要，验证码一次性消费；单条验证码最多允许 5 次错误尝试，发信与验证还受来源 IP 和邮箱限流。重启服务不会清空有效验证码记录，但更换服务端签名密钥会使原摘要无法匹配。

| 流程 | 请求验证码 | 使用验证码 | 成功后的状态 |
| --- | --- | --- | --- |
| 邮箱注册 | `POST /registration-code/request`，传 `email` | `POST /register`，传 `name`、`email`、`password`、`code` | 创建普通学员账号、邮箱标记已验证，返回个人 token；不会创建老师或管理员权限 |
| 验证码登录 | `POST /email-code-login/request`，传 `email` | `POST /email-code-login/confirm`，传 `email`、`code` | 已存在、已启用、邮箱已验证的用户登录，轮换个人 token；不会自动注册新用户 |
| 忘记密码 | `POST /password-reset/request`，传 `email` | `POST /password-reset/confirm`，传 `email`、`code`、`newPassword` | 为已启用且邮箱已验证的用户重置密码并返回新 token，旧 token 失效 |

用户名/邮箱加密码的登录方式继续可用。登录后绑定或验证邮箱另走 `POST /email-verification/request` 与 `POST /email-verification/confirm`，要求已登录；这不是公开注册入口。

### 同步发信与后台投递

注册验证码接口**同步**完成 SMTP 提交：只有 SMTP 调用完成且验证码记录成功保存后才返回 `202` 和 `{"ok":true}`。该响应证明邮件服务器接受了本次提交，不证明目标收件箱已收到，也不排除垃圾邮件或服务商后续退信。

主机或发件地址未配置时返回 `503 / MAIL_NOT_CONFIGURED`；邮件安全方式或认证字段组合无效时返回 `500 / MAIL_CONFIG_INVALID`；SMTP 投递失败时返回 `502 / MAIL_DELIVERY_FAILED`。同步发信或保存失败会回滚本次验证码变更；修正后重新请求，频率限制不会因投递失败而被绕过。

验证码登录、找回密码和登录后的邮箱验证使用后台投递。它们会先同步校验邮件配置，再返回统一的受理响应；验证码登录和找回仅向已启用且已验证邮箱的已有账号发信。未知邮箱同样收到受理响应，用于避免公开探测账号。后台 SMTP 失败只记录异常类型，`202` 不能证明发信完成。

## 上线与实收验收

1. 确认目标服务读取自己的 `AOHUANG_MAIL_*` 配置、发信域已在 Resend 验证，且新版本已实际部署；检查只展示非敏感状态。
2. 调用服务内部 `email_service.verify_connection()`，验证 DNS/网络、TLS 和认证；它不发信，也不是公开 API。
3. 使用本人可访问的新测试邮箱，在软件登录页请求注册码，检查收件箱和垃圾邮件，输入收到的验证码完成注册；确认登录后的软件用户信息和普通学员权限。
4. 退出后用该邮箱请求登录验证码，实收并完成登录；检查新 token 生效、旧 token 失效以及其他软件用户数据不混入。
5. 退出后用忘记密码流程，实收验证码并设置新密码；确认新密码可登录、旧密码和旧 token 失效，验证码重复使用失败。

2026-11-03 的生产实收与 API 验收已完成前三条用户流程：用户确认上述脱敏邮箱收到三封邮件，输入实际验证码后注册返回 201、验证码登录返回 200、密码重设返回 200，随后新密码登录返回 200，重设前 token 与匿名私有接口均返回 401。实际桌面客户端另行完成密码登录及设置验收，详见 [account-cache-acceptance-2026-11-03.md](account-cache-acceptance-2026-11-03.md)。三封验证码在本轮经生产 API 消费，未声称三次提交均通过 GUI 点击，也未将此作为旧密码失效、验证码重放和跨用户隔离的完整生产复测；后者需结合相应自动化回归判断。

以上三个用户流程的实收与操作结果应分别记录。替身 SMTP 的自动化测试、端口连通、SMTP 登录或接口 `202` 均不能代替端到端验收。后台投递故障排查使用注册验证码的同步返回、Resend 投递记录和仅含异常类型的应用日志，排查时不打印邮箱验证码或服务凭据。

当前没有公开的 SMTP 测试/诊断 API。以上注册验证码请求是应用级验证方式；测试请使用自己可访问的邮箱，确认收到邮件后再完成验证。
