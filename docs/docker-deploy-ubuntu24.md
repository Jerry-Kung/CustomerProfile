# Ubuntu 24.04 上使用 Docker 部署本服务（详细指南）

> Docker 系统准备步骤仍适用。当前平台、任务范围和配置迁移以 [统一 Worker 配置](worker-configuration.md) 为准；不要沿用历史固定 Worker 分工或商用门槛。

本文面向**首次**在 Linux 上用 Docker 部署 Python 微服务的同学，环境以 **Ubuntu 24.04 LTS** 为例，覆盖：安装 Docker、配置环境、改端口、防火墙放通、以及**前端如何填写后端 Base URL**。

> 通用说明仍见：[deploy-and-run.md](deploy-and-run.md)  
> 接口路径与轮询见：[frontend-api-guide.md](frontend-api-guide.md)

---

## 1. 架构说明（部署前要清楚）

| 组件 | 运行方式 | 是否进 Docker |
|------|----------|----------------|
| **本仓库 API** | `docker compose` 中的 `api` 服务 | ✅ |
| **本仓库 Worker** | `worker`（本地）+ `commercial-worker`（商用）+ `profile-runner-worker`（画像生成服务适配） | ✅ |
| **MySQL** | 业务数据库 | ❌ 默认**外部**（云 RDS 或自建机上的 MySQL） |
| **Dify / 擎天** | 工作流引擎（由 `WORKFLOW_PLATFORM` 选择） | ❌ 默认**外部** |
| **独立画像生成服务** | 由 `PROFILE_RUNNER_BASE_URL_PRODUCTION` 指定地址 | ❌ 默认**外部** |

容器内的 **Worker** 需要能访问：

- `MYSQL_HOST`（若 MySQL 在宿主机上，见下文「宿主机 MySQL」）
- 所选工作流平台的地址（一般为内网或公网 URL）

---

## 2. 在 Ubuntu 24.04 上安装 Docker

### 2.1 卸载旧包（若有）

```bash
sudo apt-get remove -y docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc 2>/dev/null || true
```

### 2.2 使用官方仓库安装 Docker Engine（推荐）

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "${VERSION_CODENAME:-$VERSION_ID}") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

### 2.3 验证

```bash
sudo docker run --rm hello-world
docker compose version
```

### 2.4 当前用户免 sudo（可选）

```bash
sudo usermod -aG docker "$USER"
# 重新登录 SSH 会话后生效
newgrp docker   # 或退出 SSH 再登录
```

---

## 3. 获取项目代码与配置 `.env`

```bash
cd /opt   # 或你喜欢的目录
# 若使用 git：
# git clone <你的仓库地址> customer_profile_backend
cd customer_profile_backend

cp .env.example .env
nano .env   # 或用 vim
```

**必填**：`MYSQL_*`，各 Worker 声明范围内的擎天密钥、商用目标的
`QINGTIAN_COMMERCIAL_API_KEY_*`、Runner 地址 `PROFILE_RUNNER_BASE_URL_PRODUCTION`，
以及启用失败通知/邀约核验时所需的业务 M2M 地址与 Key。默认 Compose 已分别指定平台，
完整字段见[统一 Worker 配置](worker-configuration.md)。

**Docker 下建议同时检查**：

- `LOG_LEVEL=INFO`（默认即可）
- 若 MySQL 跑在**同一台 Ubuntu 宿主机**上：
  - 容器里 **不能** 用 `MYSQL_HOST=127.0.0.1`（那会指向容器自己）。
  - 应使用 **宿主机在 Docker 网桥上的地址**，常用写法：
    - `MYSQL_HOST=host.docker.internal`（需较新 Docker 且已启用，见下）
    - 或 `MYSQL_HOST=172.17.0.1`（默认 `docker0` 网关，部分环境适用）
    - 或把 MySQL 绑定到 `0.0.0.0` 并使用**宿主机局域网 IP**
  - Compose 已为 API、迁移和 Worker 配置 `host.docker.internal:host-gateway`。
    MySQL 也须监听容器可达的网桥/局域网接口，不能仅监听宿主机 loopback。
    在 `.env` 中设 `MYSQL_HOST=host.docker.internal`，并为应用账户授权容器来源。

---

## 4. 数据库迁移

无需手工逐个执行 SQL。Compose 的 `migrate` 一次性服务会按文件名顺序执行全部幂等
迁移（当前 001–009，包括重试状态、目标绑定和旧单目标重试修复），成功后 API 和三个
Worker 才会启动。已有环境按[升级顺序](worker-configuration.md#重试与升级顺序)统一替换消费者。

---

## 5. 构建并启动服务

```bash
cd /opt/customer_profile_backend   # 项目根目录，含 Dockerfile 与 docker-compose.yml

docker compose up -d --build
```

查看状态与日志：

```bash
docker compose ps
docker compose logs -f api
docker compose logs -f worker
docker compose logs -f commercial-worker
docker compose logs -f profile-runner-worker
```

健康检查（默认映射宿主机 **18008** 时）：

```bash
curl -sS http://127.0.0.1:18008/health
```

---

## 6. 端口冲突：如何修改对外端口

对外访问的是 **宿主机端口 → 容器内 18008** 的映射，由环境变量 **`HOST_API_PORT`** 控制（见 `docker-compose.yml`）。

### 6.1 写入 `.env`（推荐，便于记住）

在 `.env` 中增加或修改：

```env
HOST_API_PORT=18080
```

然后重建并启动：

```bash
docker compose up -d --build
```

此时本机访问：

```bash
curl -sS http://127.0.0.1:18080/health
```

### 6.2 仅当前 shell 临时指定

```bash
export HOST_API_PORT=18080
docker compose up -d
```

### 6.3 说明：`API_PORT` vs `HOST_API_PORT`

| 变量 | 含义 |
|------|------|
| `HOST_API_PORT` | **宿主机**上对外监听的端口（给浏览器 / 前端 / 公网访问） |
| `API_PORT`（容器内） | 进程在**容器内**监听端口，默认 `18008`；Compose 会同步修改映射右侧及 readiness 检查端口 |

---

## 7. 防火墙与安全组：放通端口

### 7.1 Ubuntu 本机防火墙 UFW

若已启用 UFW：

```bash
sudo ufw status
```

放通你实际使用的宿主机端口（与 `HOST_API_PORT` 一致），例如 **18080**：

```bash
sudo ufw allow 18080/tcp comment 'customer-profile-api'
sudo ufw reload
sudo ufw status numbered
```

**仅内网访问**时，可限制来源（示例：仅某网段）：

```bash
sudo ufw allow from 10.0.0.0/8 to any port 18080 proto tcp
```

### 7.2 云服务器安全组（阿里云 / 腾讯云 / AWS 等）

在控制台为实例绑定安全组，**入站规则**增加：

- 协议：TCP  
- 端口：`HOST_API_PORT`（如 `18080`）  
- 来源：你的办公网 / `0.0.0.0/0`（演示环境；生产建议收紧）

**UFW 与云安全组都要放行**时，两者需一致，否则仍无法从外网访问。

### 7.3 确认监听地址

容器内服务监听 `0.0.0.0:18008`，映射到宿主机后，应可通过 **服务器公网 IP + 端口** 访问（在安全组与 UFW 均允许的前提下）。

---

## 8. 部署后：前端如何配置 Base URL

前端所有接口都相对于**同一个 HTTP(S) 根地址**，例如：

```text
http://<服务器IP或域名>:<HOST_API_PORT>
```

### 8.1 示例

- 服务器公网 IP：`203.0.113.50`  
- `HOST_API_PORT=18080`  

则前端环境变量可设为：

```text
VITE_API_BASE_URL=http://203.0.113.50:18080
# 或 axios baseURL 同上
```

具体请求示例：

- `GET  http://203.0.113.50:18080/health`
- `POST http://203.0.113.50:18080/v1/customer-profile/tasks`
- `GET  http://203.0.113.50:18080/v1/customer-profile/tasks?phone_prefix=131&page=1&page_size=20`
- `GET  http://203.0.113.50:18080/v1/customer-profile/tasks/13129509529/result`

（路径中的手机号需 URL 编码若含特殊字符。）

### 8.2 使用域名 + HTTPS（推荐生产）

前面板用户浏览器通常要求 **HTTPS**。做法之一：

1. 在同一台机或另一台机用 **Nginx / Caddy** 做反向代理：  
   `https://api.example.com` → `http://127.0.0.1:18080`
2. 前端 Base URL 设为：`https://api.example.com`（**不带**末尾路径 `/v1/...`，由前端拼接）。

### 8.3 跨域（CORS）说明

后端内置 CORS 中间件，`CORS_ALLOW_ORIGINS` 默认 `*`，也可用逗号分隔的业务域名列表。
HTTPS 页面调用 HTTP 接口还会受到浏览器混合内容限制，应保持前后端 HTTPS 配置一致。

### 8.4 图片字段与 Docker

当前提交接口不接收图片字段，上传/TOS 链路未启用，Compose 不再挂载上传卷。
实际表单字段以[前端指南](frontend-api-guide.md)和 OpenAPI 契约为准。

---

## 9. 常用运维命令

```bash
# 停止
docker compose down

# 更新代码后重新构建并启动
docker compose build --no-cache
docker compose up -d

# 查看资源占用
docker stats
```

---

## 10. 快速检查清单

- [ ] Docker 与 `docker compose` 可用  
- [ ] `.env` 中 MySQL、擎天、Runner 及业务 M2M 地址对容器可达（注意 `127.0.0.1` 陷阱）
- [ ] `migrate` 已成功执行全部 `001`–`009` 迁移
- [ ] `HOST_API_PORT` 无冲突，且 **UFW + 云安全组** 已放通  
- [ ] `curl http://127.0.0.1:<HOST_API_PORT>/ready` 返回 HTTP 200、数据库 `up`
- [ ] 前端 Base URL = `http(s)://<主机>:<HOST_API_PORT>`（或反代后的域名）

按上述配置后，前端即可用该 Base URL 调用 [frontend-api-guide.md](frontend-api-guide.md) 中的三个业务接口。
