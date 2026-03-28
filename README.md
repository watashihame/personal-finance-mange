# 投资持仓追踪

个人投资组合管理 Web 应用，支持 A 股、美股、日股多市场持仓记录，自动抓取行情，以人民币汇总总资产。

## 功能

- **多市场支持** — A 股（沪深）、美股、日股、加密货币
- **自动行情** — A 股通过 [Tushare Pro](https://tushare.pro) 获取，美股/日股/加密通过 yfinance 获取
- **汇率换算** — 自动获取 USD/JPY 对 CNY 汇率，所有持仓以人民币汇总
- **手动价格** — 可为任意标的手动设置价格，屏蔽自动抓取
- **标签分类** — 为持仓添加自定义标签，支持按标签筛选
- **可视化** — 资产分配饼图 + 持仓市值柱状图
- **Docker 部署** — 开箱即用的 Docker Compose 配置（Flask + Nginx + PostgreSQL）

## 快速开始

### 前置条件

- [Docker](https://docs.docker.com/get-docker/) 及 Docker Compose
- [Tushare Pro](https://tushare.pro) 账号（用于 A 股行情，免费注册）

### 部署步骤

**1. 克隆仓库**

```bash
git clone https://github.com/your-username/personal-finance.git
cd personal-finance
```

**2. 配置环境变量**

```bash
cp .env.example .env
```

编辑 `.env`，填入以下必填项：

```ini
POSTGRES_PASSWORD=your_strong_password
SECRET_KEY=your_random_32char_secret
TUSHARE_TOKEN=your_tushare_token
```

**3. 启动**

```bash
docker compose up -d --build
```

**4. 访问**

打开浏览器访问 `http://localhost`（或服务器 IP）。

---

### 本地开发（不使用 Docker）

```bash
pip install -r requirements.txt

# 使用 SQLite，无需 PostgreSQL
python app.py
```

访问 `http://localhost:5000`。A 股行情需在 `config.ini` 中配置 Tushare Token：

```ini
[tushare]
token = your_tushare_token_here
```

## 标的代码格式

| 市场 | 格式 | 示例 |
|------|------|------|
| A 股（沪） | `代码.SH` | `600519.SH` |
| A 股（深） | `代码.SZ` | `000001.SZ` |
| 美股 | ticker | `AAPL` |
| 日股 | `代码.T` | `7203.T` |
| 加密货币 | `代码-USD` | `BTC-USD` |

## 架构

```
浏览器
  └─ Nginx :80
       ├─ /static/  →  直接返回静态文件
       └─ /         →  反向代理到 Gunicorn
                          └─ Flask 应用
                               └─ PostgreSQL
```

| 容器 | 镜像 | 说明 |
|------|------|------|
| `nginx` | nginx:1.27-alpine | 反向代理 + 静态文件 |
| `web` | 本地构建 | Flask + Gunicorn，2 workers |
| `db` | postgres:16-alpine | 数据持久化 |

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `POSTGRES_PASSWORD` | 是 | — | PostgreSQL 密码 |
| `SECRET_KEY` | 是 | — | Flask Session 密钥 |
| `TUSHARE_TOKEN` | 否 | — | A 股行情 Token，不填则 A 股无法自动刷新 |
| `POSTGRES_DB` | 否 | `portfolio` | 数据库名 |
| `POSTGRES_USER` | 否 | `portfolio` | 数据库用户名 |
| `NGINX_PORT` | 否 | `80` | Nginx 监听端口 |

## 常用命令

```bash
# 查看日志
docker compose logs -f web

# 重新构建并更新应用
docker compose up -d --build web

# 备份数据库
docker compose exec db pg_dump -U portfolio portfolio > backup_$(date +%Y%m%d).sql

# 恢复数据库
docker compose exec -T db psql -U portfolio portfolio < backup.sql

# 停止所有服务
docker compose down
```

## 数据库迁移说明

如果从旧版本升级（新增了 `tags` 字段），需手动执行：

```sql
ALTER TABLE holdings ADD COLUMN tags VARCHAR(200) DEFAULT '';
```

全新部署无需此操作，`init_db()` 启动时自动建表。

## API

应用提供 JSON API 供外部脚本或二次开发使用，详见 [API.md](API.md)。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/refresh-prices` | POST | 刷新所有持仓行情 |
| `/api/override-price` | POST | 手动设置价格 |
| `/api/clear-override` | POST | 清除手动价格 |
| `/api/portfolio-data` | GET | 获取图表数据（JSON） |

## 技术栈

- **后端** — Python 3.12, Flask 3.1, SQLAlchemy 2.0, Gunicorn
- **数据库** — PostgreSQL 16（开发环境可用 SQLite）
- **行情** — [Tushare Pro](https://tushare.pro)（A 股）, [yfinance](https://github.com/ranaroussi/yfinance)（美股/日股/加密）
- **前端** — Jinja2 模板, Bootstrap 5.3, Chart.js 4
- **部署** — Docker, Docker Compose, Nginx

## License

Copyright (c) 2026 watashihame. Released under the [MIT License](LICENSE).
