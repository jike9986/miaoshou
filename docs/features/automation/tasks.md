# 自动化处理 · 任务清单

## 已完成（v0.1.24，2026-11-17）

- [x] 批次进度卡在 50%：`imagePlanned=0` 时 imagePercent 返回 100%
- [x] SKU 颜色/尺码显示 hex ID：改用全局 `flatIdToLabel` Map
- [x] 成品仓库不刷新：运行结束时 `invalidateQueries({ queryKey: queryKeys.warehouse.all })`
- [x] 编辑后商品消失：`review_product_quality` / `update` / `patch_product_images` 同步 `draft_verified_revision`
- [x] 重试报"已开始"：`start_item` 接受 `failed | needs_review` 状态
- [x] 执行历史与进度面板状态不一致：同上，warehouse cache 刷新修复
- [x] 页面滚动缩放：`main.tsx` 全局拦截 Ctrl+Scroll

## 已完成（v0.1.25，2026-11-17）

- [x] 批次进度完成后显示 50%：`effectivelyDone` 时强制 batchPercent=100，防止子进度短暂归零拉低平均值
- [x] 图片处理进度完成后归零：`effectivelyDone && imagesEnabled` 时强制 imagePercent=100，label 改为"已完成"而非"无需翻译"
- [x] 进度条一次跳 10-20%：改用 setInterval 1%/50ms 匀速逼近，视觉上逐格计数；Progress 条 CSS transition 从 700ms 缩短为 150ms

## 待完成

## 已完成（v0.1.25，2026-11-17）

- [x] 图片写回妙手并发控制：服务端加 `threading.Semaphore`，上限 = 前端"并行件数"（最高 5）。20 件并发时图片线程不再全部同时下载图片/写妙手，避免 OOM（此前 20 件 11 个失败的原因就是服务器 8GB 内存被 OOM-kill，不是妙手限流）。前端在 `bg_image_job` 里传 `itemConcurrency: parallel`，服务端在 `_spawn_image_translation` 前用 `_reset_image_semaphore` 重置。
- [x] 服务重启后"正在恢复"卡死：`main.py` 启动时调 `recover_interrupted_runs_on_startup()`，把所有 `process_status/publish_status = running` 的 item 重置为 `failed`，并重算 run 状态。OOM/部署重启后不再卡死。

- [x] 执行历史不能滚动：`max-h-full` 在无固定父高度时失效，改为 `maxHeight: 16rem` inline style
- [x] 图片进度提交翻译后仍显示"识别中"：`imagePlanned > 0` 但 `imageDone === 0` 时改为"翻译中，等待结果"
- [x] 批次进度先到100图片进度还在走：`batchPercent` 锁定100%需要 `imagesDone` 也为真（`imageDone >= imagePlanned`），不再仅依赖 `effectivelyDone`
- [x] 妙手接口 504 超时（5 并发）：`request_timeout` 30s 过短，5 件 × 4 次调用 × 1.5s 限流 ≈ 30s 踩线；改为 90s
- [x] 颜色全部无法识别（"网面"/"布面"）：颜色属性值是材质描述而非颜色词时，`clean_color_strict` 全部失败导致整件失败；`normalize_colors` 在所有颜色值都识别不出时回退到原样保留（去空白），不再整件失败

- [ ] 断点续跑：关闭应用后重开，`running` 状态的 item 自动重置为 `queued`（目前需手动重试）
- [ ] 并行数上限提示：超过妙手节流限制（1.5s/账号）时，系统没有提示用户降低并行数
- [ ] 运行超时保护：单个 item 长时间停在 `running` 没有自动超时机制
- [ ] 文字已完成时跳过重处理：重试时若商品已入仓且 draft_verified_revision=revision，跳过文字处理阶段直接进入图片
- [ ] 图片已提交时跳过重提交：重试时若上次图片已提交但未写回，直接拉取结果写回，不重新提交翻译

## 相关 Bug 记录

| ID | 现象 | 原因 | 修复版本 |
|---|---|---|---|
| BUG-001 | 批次进度永远 50% | `imagePlanned=0` → `imagePercent=0` | v0.1.24 |
| BUG-002 | SKU 乱码 | skuMap key 顺序与维度顺序不同 | v0.1.24 |
| BUG-003 | 自动化结果页消失商品 | `revision_guard` 递增 `revision` 但 `draft_verified_revision` 未跟进 | v0.1.24 |
| BUG-004 | 重试报"已开始" | `start_item` 只接受 `queued` | v0.1.24 |
| BUG-005 | 仓库不刷新 | 运行结束时未 invalidate `warehouse.all` | v0.1.24 |
| BUG-006 | 完成后批次进度显示 50% | `effectivelyDone` 时 batchPercent 未锁定为 100%，子进度归零拉低平均 | v0.1.25 |
| BUG-007 | 完成后图片进度归零并显示"无需翻译" | `effectivelyDone` 时 imagePercent 未锁定 100%，状态重置后显示 0% | v0.1.25 |
| BUG-008 | 进度条一次跳 10-20% | rAF 一步跳到目标值；改为 setInterval 1%/50ms 匀速计数 | v0.1.25 |
| BUG-009 | 等待开始时进度条显示 50% | phase=idle 时 imagePercent 触发"无需翻译=100%"分支，(0+100)/2=50 | v0.1.25 |
| BUG-010 | 价格表颜色/尺码列颠倒 | skuMap key 按 ID 升序，dims 数组位置与维度顺序不符；改用 idToDimIndex 映射 | v0.1.25 |
| BUG-012 | 重试时报"正在处理中" | 重启后 `recover_interrupted_runs_on_startup` 重置 items 为 failed 但未清 `automation_images.queued`，`_resume_stuck_image_jobs` 重新拉起图片线程持锁，重试被挡 | v0.1.26 |
| BUG-013 | 重试时报"图片仍在处理" | 进程被杀后 `automation_images.queued=True` 残留，线程已死但 guard 仍触发；获取到锁后直接重置状态而非 busy | v0.1.26 |
| BUG-014 | 颜色属性为材质词（"网面"/"布面"）导致整件失败 | `clean_color_strict` 识别不出任何颜色词时整件抛错；改为回退到原样保留，让后续流程继续 | v0.1.26 |
| BUG-015 | 颜色上限对一半商品无效 | `apply_image_limits` 写死 `attrId == "100000"` 认颜色维度，生产库一半商品该字段是 `None`/空串，颜色截断被静默跳过 | v0.1.28 |
| BUG-016 | 颜色删了但 SKU 全留着（孤儿 SKU） | 用 `attrValueId` 比对 `skuMap` 的 key，但真实 key 是 `;颜色值文本;尺码值文本;`，永远匹配不上 | v0.1.28 |
