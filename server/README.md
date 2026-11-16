# 奥皇上品后端

FastAPI + PostgreSQL，为 Electron 桌面客户端提供 REST API。

## 目录结构

```
server/
├── api/            路由层（HTTP 接口定义，不含业务逻辑）
│   ├── __init__.py 路由汇总 + 鉴权依赖挂载
│   ├── auth.py     登录、注册、连接妙手账号
│   ├── warehouse.py 成品仓库 CRUD
│   ├── collect_box.py 采集箱（妙手上游）
│   ├── images.py   图片翻译任务
│   ├── publish.py  发布（上架到妙手）
│   ├── schedules.py 定时发布任务
│   ├── batch.py    批量操作
│   ├── operators.py 操作员管理
│   └── ...
├── services/       业务逻辑层
│   ├── warehouse_service.py    仓库查询、入库、改类目
│   ├── collect_box_service.py  采集箱分页、认领
│   ├── publish_service.py      发布流程、批次管理
│   ├── image_service.py        图片翻译队列
│   ├── schedule_service.py     定时任务 CRUD + 到期判定
│   ├── account_service.py      妙手账号绑定
│   ├── operator_service.py     操作员（老师/学员）
│   ├── price_template.py       定价模板 + SKU 售价计算
│   └── ...
├── models/         SQLAlchemy ORM 模型
├── schemas/        Pydantic 请求/响应模型
├── core/
│   ├── config.py   环境变量 + 路径配置
│   ├── constants.py 业务常量（上限、枚举等）
│   ├── errors.py   统一错误码 + AppError
│   └── utils.py    通用工具函数
├── db/
│   └── session.py  数据库连接（SQLAlchemy）
└── main.py         FastAPI 应用入口
```

## 分层约定

- **api/** 只做：解析请求、调 service、返回响应、写审计日志
- **services/** 负责：业务规则、数据库读写、调妙手 API
- **models/** 只定义表结构，不含业务方法
- 跨 service 调用可以，但不能跨 api 调用另一个 api

## 鉴权

所有业务路由经过两道依赖（`api/__init__.py` 的 `_guarded`）：

1. `require_token`：令牌格式校验 + 有效性
2. `current_operator`：解析成操作员对象 + 拦截已停用账号

免鉴权的路由：`GET /system/health`、`POST /auth/login`、`POST /auth/register` 等（见 `api/__init__.py`）。

## 角色权限

| 角色 | 说明 |
|------|------|
| admin | 最高权限，可管理所有数据和操作员 |
| teacher | 老师，可操作所有仓库数据，可绑定学员账号 |
| member | 学员，只能看成品仓库、认领商品、发布自己认领的商品 |

## 关键业务概念

**采集箱**：妙手 ERP 里的商品暂存区，商品在这里等待被认领入库。

**认领**：把采集箱里的商品绑定到某个学员，作为该学员待处理的原始货源。

**入仓**：把认领的商品经过处理（翻译图片、填重量等）后正式保存到成品仓库。

**发布**：把成品仓库里的商品上架到 TikTok Shop 店铺。

## 本地开发

```bash
# 安装依赖
pip install -r requirements.txt

# 启动开发服务器
python -m uvicorn main:app --reload --port 8812

# 访问 API 文档（需 AOHUANG_DEV=1）
http://127.0.0.1:8812/api/docs
```

环境变量：复制 `.env.example` 为 `.env` 并填写。

## 运行测试

```bash
pytest tests/ -v
```
