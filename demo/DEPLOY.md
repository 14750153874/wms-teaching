# WMS 上线准备说明（预生产检查单）

> 适用范围：`demo/` 下的 Python 后端（`demo/backend/app.py`、单体 `demo/app.py`）与静态前端（`demo/frontend/`）。
> 与《项目方案与技术文档 V2.0》第 6—8 章（部署 / 运维 / 安全）对应。
> **当前尚未部署到服务器**：本文件把上线前必须完成的事项固化下来，逐项可执行、可验证。

---

## 一、环境变量与密钥（已从代码抽离）

### 1. 文件与优先级

| 文件 | 用途 | 是否进代码库 |
| --- | --- | --- |
| `demo/.env.example` | 变量模板（含注释说明） | ✅ 提交 |
| `demo/.env` | 本地开发实际配置（含随机密钥） | ❌ 不提交（`.gitignore` 已忽略） |

**加载优先级：系统环境变量 > `.env` 文件。** 因此线上可以直接用系统环境变量注入，不依赖 `.env`。
后端启动时会就近查找 `.env`：先找 `backend/` 目录，再找上一级 `demo/` 目录（独立部署时把 `.env` 放在 `backend/` 同级即可）。

### 2. 变量清单

| 变量 | 说明 | 开发默认 | 生产要求 |
| --- | --- | --- | --- |
| `WMS_ENV` | 运行环境：`development` / `production` | `development` | `production` |
| `WMS_HOST` | 监听地址 | `127.0.0.1` | `127.0.0.1`（Nginx 反代） |
| `WMS_PORT` | 监听端口 | `8000` | `8000` |
| `WMS_SECRET_KEY` | 登录 token 的 HMAC 签名密钥（等效 API 密钥） | 开发默认值（会告警） | **必填**，≥32 字节，各环境独立 |
| `WMS_TOKEN_EXPIRE_HOURS` | 登录有效期（小时） | `8` | 按安全策略调整 |
| `WMS_CORS_ORIGINS` | 允许跨域的前端来源，逗号分隔 | `*` | 收敛为真实域名白名单 |
| `WMS_DB_PATH` | SQLite 库文件路径 | `data/wms.db` | 按部署目录调整 |
| `WMS_DB_HOST/PORT/USER/PASSWORD/NAME` | MySQL 预留（当前未启用） | — | 接入 MySQL 时填写 |

### 3. 生产密钥生成与轮换

```bash
# 生成 ≥32 字节随机密钥（文档 6.3.6 指定方式）
openssl rand -base64 48
```

- 各环境使用不同密钥；**每个环境独立生成**，禁止复用；
- 线上 `.env` 权限 600：`chmod 600 .env`（属主 root 或专用运维账号）；
- 密钥每年轮换一次，运维人员变动时立即轮换；
- 更换密钥后所有旧 token 失效（401），属预期行为。

### 4. 线上注入方式（任选其一）

```bash
# 方式 A：systemd（EnvironmentFile 指向 .env，权限 600）
# /etc/systemd/system/wms-api.service
[Service]
WorkingDirectory=/opt/wms/demo/backend
EnvironmentFile=/opt/wms/demo/.env
ExecStart=/usr/bin/python3 app.py --host 127.0.0.1 --port 8000

# 方式 B：docker compose
# extra_hosts / env_file: ../.env

# 方式 C：手工 export（临时验证用）
export WMS_ENV=production
export WMS_SECRET_KEY="$(openssl rand -base64 48)"
```

### 5. 已内置的安全约束（代码层）

- `WMS_ENV=production` 且 `WMS_SECRET_KEY` 缺失或短于 32 字节 → **拒绝启动**（已实测）；
- 开发环境缺失密钥时打印告警，保证演示可零配置运行；
- 操作日志脱敏：`password / secret / token / apikey` 等字段写库前置为 `***`（对应文档 6.3.6"日志屏蔽密码类字段"）；
- CORS 按 `WMS_CORS_ORIGINS` 白名单回显来源（`*` 仅限本地开发）。

---

## 二、后端启动命令（已实测）

```powershell
# 开发（自动读取 .env；默认 127.0.0.1:8000）
python app.py

# 本机/局域网演示（对外监听，必须配合防火墙）
python app.py --host 0.0.0.0 --port 8000

# 生产推荐：仅本机监听，由 Nginx 反代（先设置 WMS_ENV=production 与 WMS_SECRET_KEY）
python app.py --host 127.0.0.1 --port 8000

# 重置演示数据
python app.py --reset
```

- 命令行参数优先于环境变量：`--host` / `--port` 覆盖 `WMS_HOST` / `WMS_PORT`；
- 启动 banner 会打印当前环境、`.env` 路径、密钥来源与数据库路径，便于部署时核对：
  ```
  环境   : production（配置文件 /opt/wms/demo/.env）
  密钥   : 已通过环境变量/.env 配置
  ```
- 健康检查：`GET /api/v1/health`；根路径 `GET /` 返回服务信息（含当前环境）。

---

## 三、前端（npm 静态文件）

目录：`demo/frontend/`（纯静态 HTML/CSS/JS，无框架）。

```powershell
cd demo\frontend

npm ci            # 严格按 package-lock.json 安装（保证版本一致）
npm start         # 本地静态服务：http://127.0.0.1:8001（serve）

npm run build     # 产出 dist/（4 个静态文件），供 Nginx 等静态服务器发布
npm run preview   # 预览 dist/ 产物
```

- **线上发布**：把 `dist/` 内容交给 Nginx 站点根目录即可（对应文档 6.3：服务器不安装 Node）；
- **无 Node 回退**：`python -m http.server 8001 --directory frontend`（零依赖演示用）；
- 前端 API 地址目前写死本机地址（`frontend/config.js`），按约定**本次不处理**，上线前替换为正式域名。

---

## 四、依赖锁定（保证线上依赖版本一致）

| 端 | 锁定物 | 说明 |
| --- | --- | --- |
| 前端 | `package.json`（精确版本 `serve@14.2.4`）+ `package-lock.json`（lockfileVersion 3） | 生产/CI **一律使用 `npm ci`**；升级依赖必须同时提交锁文件 |
| 后端 | `backend/requirements.txt`（当前为标准库实现，无第三方依赖） | 部署固定执行 `python -m pip install -r requirements.txt`（空操作，保持流程一致） |

- Python 运行基线：**3.12.10**（本机已验证；最低兼容 3.9+），部署时固定到同一小版本；
- 任何新增第三方依赖都必须写死精确版本（`==`），保证各环境一致。

---

## 五、防火墙（现在就做，暂不部署）

### Linux 服务器（Ubuntu 22.04 + ufw）

```bash
cd demo/deploy

sudo bash firewall-ufw.sh --dry-run   # 先预览将执行的命令
sudo bash firewall-ufw.sh             # 应用：默认拒绝入站，仅放行 SSH/80/443
```

- SSH 端口默认 22022，可用环境变量覆盖：`SSH_PORT=22 sudo -E bash firewall-ufw.sh`；
- **云控制台安全组必须同步收敛**（双层防护，文档 7.4）；
- 应用端口 8000 只允许本机（Nginx 反代），不对公网放行；
- 验证：`ufw status verbose`、`ss -lntp`（应只见 80、443、SSH 及本机回环）、外网端口扫描。

### Windows（本机 / 演示机）

```powershell
# 管理员 PowerShell
powershell -ExecutionPolicy Bypass -File demo\deploy\firewall-windows.ps1

# 查看 / 移除
Get-NetFirewallRule -DisplayName 'WMS-*' | Format-Table DisplayName,Enabled,Direction,Action
powershell -ExecutionPolicy Bypass -File demo\deploy\firewall-windows.ps1 -Remove
```

- 效果：8000/8001 仅"专用网络 + 本地子网"可入站；显式阻断公网（Public）入站；
- 脚本幂等（先删后建），可重复执行。

---

## 六、上线前检查清单

| # | 检查项 | 验证方式 |
| --- | --- | --- |
| 1 | 生产密钥已生成并注入（≥32 字节，各环境独立） | 启动 banner 显示"密钥：已通过环境变量/.env 配置" |
| 2 | `WMS_ENV=production` 且密钥缺失时拒绝启动 | 实测已通过（退出码 1 + FATAL 提示） |
| 3 | `.env` 权限 600、未进代码库 | `stat -c %a .env` → 600；`git log --all -- .env` 为空 |
| 4 | CORS 已收敛为真实域名 | `WMS_CORS_ORIGINS=https://your-domain` 后用其他来源请求验证被拒 |
| 5 | 防火墙：仅 3 个端口对外 | `ufw status verbose` + 外网端口扫描 |
| 6 | 后端只监听 127.0.0.1 或不直接暴露 | `ss -lntp` / `netstat -ano` |
| 7 | 前端 `dist/` 为 `npm ci && npm run build` 产物 | 比对文件与构建日志 |
| 8 | 依赖锁定生效 | `npm ci` 无警告变更；后端空依赖说明一致 |
| 9 | 健康检查接口可访问 | `GET /api/v1/health` 返回 200 |
| 10 | 回归脚本通过 | `python check_local_demo.py` → `[OK] ... 已通过` |

---

## 七、本次改动清单

| 文件 | 改动 |
| --- | --- |
| `demo/.env.example` / `demo/.env` | 新增：环境变量模板 / 本地配置（含随机密钥） |
| `demo/backend/app.py` | 密钥、DB 路径、端口、CORS、token 有效期改为环境变量注入；生产密钥校验；日志脱敏 |
| `demo/app.py` | 同上（单体演示版同步） |
| `demo/frontend/package.json` / `package-lock.json` | 新增：npm 静态服务 + 构建脚本 + 锁文件 |
| `demo/frontend/scripts/build.mjs` | 新增：`npm run build` → `dist/` |
| `demo/backend/requirements.txt` | 新增：依赖清单与锁定说明（标准库实现） |
| `demo/deploy/firewall-ufw.sh` | 新增：Linux（ufw）防火墙脚本 |
| `demo/deploy/firewall-windows.ps1` | 新增：Windows 防火墙脚本 |
| `demo/start_separate.ps1` | 前端优先用 npm serve 启动（无 Node 时回退 Python） |
| `wms/.gitignore` | 新增：忽略 `.env`、`node_modules/`、`dist/` 等 |
