# 截图索引

`docs/` 下所有截图集中在这里，共 35 张。文档正文不再内嵌图片，
需要看图时按下面的分组找。

> 这些图是开发过程记录，**不是验收证据**。
> 详见 `docs/status.md` 末尾「关于证据」一节。

## 页面渲染（`page-*.png`，8 张）

| 文件 | 内容 |
| --- | --- |
| `page-warehouse.png` | 仓库页初版 |
| `page-warehouse-data.png` | 仓库页（带真实数据） |
| `page-warehouse-data2.png` | 仓库页（带真实数据，第二次） |
| `page-warehouse-data3.png` | 仓库页（带真实数据，第三次） |
| `page-warehouse-real.png` | 仓库页（真实接口数据） |
| `page-warehouse-imgs.png` | 仓库页（图片加载） |
| `page-warehouse-final.png` | 仓库页（最终版） |
| `page-wh-2.png` | 仓库页（窄屏） |
| `page-final.png` | 整体最终态 |

## 界面预览（`preview-*.png`，2 张）

| 文件 | 内容 |
| --- | --- |
| `preview-fresh.png` | 全新安装后的首屏 |
| `preview-shell.png` | Electron 外壳（侧栏 + 内容区） |

## 冒烟截图（`shot-*.png`，4 张）

| 文件 | 内容 |
| --- | --- |
| `shot-app.png` | 应用整体 |
| `shot-collect.png` | 采集页 |
| `shot-diagnose.png` | 诊断页 |
| `shot-running.png` | 运行中状态 |

## 其他（`translated-result.png`，1 张）

| 文件 | 内容 |
| --- | --- |
| `translated-result.png` | 标题翻译结果 |

## 各功能页（原 `docs/shots/` 下 19 张）

采集 / 图片 / 发布 / 计划任务 / 仓库 / 违规词六个功能页的历次截图：

| 分组 | 文件 |
| --- | --- |
| 采集 | `collect.png` `collect2.png` `f-collect.png` |
| 图片 | `images.png` `f-images.png` |
| 发布 | `publish.png` `publish2.png` `f-publish.png` |
| 计划任务 | `schedules.png` `schedules2.png` `f-schedules.png` `schedule-list.png` `schedule-dialog.png` `schedule-created.png` |
| 仓库 | `warehouse.png` `f-warehouse.png` |
| 违规词 | `words.png` `f-words.png` |
| 协议/环境 | `file-protocol.png` |

`f-` 前缀的是「最终版」，无前缀的是过程截图。

## 为什么集中到一处

原先图片散在 `docs/`（16 张）和 `docs/shots/`（19 张）两个目录，文件名风格
不统一（`page-*` / `preview-*` / `shot-*` / `f-*`），新增截图时不知道该放哪。
现在只有 `docs/screenshots/` 一个目录，命名保持原样（改名会让历史文档里的
文字引用失效）。
