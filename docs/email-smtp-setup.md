# 服务端邮件投递配置

邮件验证码由 FastAPI 服务端通过 SMTP 发送。配置项使用 `AOHUANG_` 环境变量前缀；SMTP 主机和发件地址必填，用户名与密码需同时配置或同时留空。

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

每次只选择一种传输安全方式。常见配置如下；将尖括号中的占位项替换为邮件服务商提供的信息，不要把密码提交到代码仓库。

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

## 验证

注册验证码接口会**同步**完成发信：只有邮件已提交且验证码记录成功保存后才返回 `202` 和 `{"ok":true}`。可以用自己的测试邮箱调用 `POST /api/v1/auth/registration-code/request`，收到邮件并完成验证码注册，验证端到端流程。服务器未配置主机或发件地址时，接口返回 `503`、错误码 `MAIL_NOT_CONFIGURED`；SMTP 连接或认证失败时返回 `502`、错误码 `MAIL_DELIVERY_FAILED`。两类失败都会回滚本次验证码记录；修正配置后重新请求即可。请求仍受 IP 与邮箱频率限制。

密码找回和登录后的邮箱绑定使用后台投递，以免把 SMTP 故障用于探测账号；它们会先返回统一的受理响应，投递失败只会在服务日志中留下异常类型。因此应使用注册验证码请求验证 SMTP 投递，不要只看找回或绑定接口的成功响应。

当前没有公开的 SMTP 测试/诊断 API。以上注册验证码请求是应用级验证方式；测试请使用自己可访问的邮箱，确认收到邮件后再完成验证。
