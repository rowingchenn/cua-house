# Prompt: 开发 CUA-House VM 中控台（noVNC Dashboard）

## 目标

开发一个 Web 中控台，让运维人员能够：
1. 一览所有 Worker 节点及其 VM 状态
2. 一键点击任意 VM，打开 noVNC 界面查看其 Windows 桌面
3. 无论 VM 是空闲 (ready) 还是正在执行任务 (leased)，都能连入

---

## 现有系统架构

### 集群拓扑

```
Client (浏览器)
    │
    ▼
Master (34.55.178.43:8787)      ← 调度/监控，不参与数据路径
    │ WebSocket 长连接
    ├── Worker kvm02 (35.188.39.143:8787)   ← 7 个 VM
    └── Worker kvm03 (136.113.232.63:8787)  ← 7 个 VM
```

- 每个 Worker 上跑多个 QEMU Windows VM，每个 VM 是一个独立 Docker 容器
- 每个容器内部固定端口：**5000**（CUA Agent Server）、**8006**（noVNC）
- GCP 防火墙只开放了 **8787** 端口，VM 的 Docker 映射端口（16000-16999, 18000-18999）**不可从外网直连**

### 已有 API 接口

#### Master API (http://34.55.178.43:8787)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/healthz` | 健康检查 |
| GET | `/v1/cluster/workers` | 列出所有 Worker 及其 VM 列表 |
| GET | `/v1/cluster/tasks` | 列出所有 task（支持 `?state=` 过滤） |
| GET | `/v1/cluster/batches` | 列出所有 batch（支持 `?state=` 过滤） |
| GET | `/v1/cluster/status` | 集群总览（含 tasks_by_state 计数） |
| GET | `/v1/cluster/pool` | 当前 pool 分配 |
| GET | `/v1/tasks/{task_id}` | 单个 task 详情 |

#### `GET /v1/cluster/workers` 返回示例

```json
[
  {
    "worker_id": "kvm02",
    "online": true,
    "capacity": {"total_vcpus": 32, "total_memory_gb": 128, ...},
    "hosted_images": ["cpu-free"],
    "vm_summaries": [
      {
        "vm_id": "32bd0ac5-3a0f-4fe5-b1d6-bf92fd5fcd92",
        "image_key": "cpu-free",
        "state": "ready",
        "lease_id": null,
        "public_host": "35.188.39.143",
        "published_ports": {"5000": 16000},
        "novnc_port": 18000,
        "vcpus": 4,
        "memory_gb": 16,
        "disk_gb": 64,
        "from_cache": true,
        "warming": false
      },
      {
        "vm_id": "9e1659db-...",
        "state": "leased",
        "lease_id": "abcd-1234-...",
        "novnc_port": 18001,
        ...
      }
    ]
  }
]
```

每个 VM 的关键字段：
- `vm_id`: 唯一标识
- `state`: `"ready"` (空闲) / `"leased"` (执行中) / `"booting"` / `"reverting"` 等
- `lease_id`: 当 state=leased 时有值，state=ready 时为 null
- `public_host`: Worker 的外网 IP
- `novnc_port`: 该 VM 的 noVNC Docker 映射端口（如 18000、18001...）
- `published_ports`: guest_port → host_port 映射

---

## noVNC 连接机制（核心）

### 子域名反向代理

每个 Worker 的 FastAPI server (8787) 内置了一个 **基于 Host header 的反向代理**。所有流量走 8787 端口，通过 Host header 中编码的子域名路由到正确的 VM。

#### URL 格式

```
http://novnc--<lease_id>.<worker_ip>.sslip.io:8787/novnc/
```

- `sslip.io` 是免费通配 DNS 服务：`anything.35.188.39.143.sslip.io` → 解析到 `35.188.39.143`
- Host header 格式：`<service>--<lease_id>.<public_base_host>`
- 代理解析 Host header 后，路由到对应 VM 的 `127.0.0.1:<novnc_port>`

#### 代理处理流程

```
浏览器请求:
  GET http://novnc--abcd1234.35.188.39.143.sslip.io:8787/novnc/

  1. DNS: sslip.io 解析 → 35.188.39.143
  2. 到达 Worker :8787 的 catch-all route
  3. parse_proxy_host() 从 Host header 解析:
     - service = "novnc"
     - lease_id = "abcd1234"
  4. resolve_proxy_target() 查 scheduler:
     - lease_id → slot_id → VMHandle → novnc_port = 18001
     - target = http://127.0.0.1:18001
  5. 路径改写: 去掉 /novnc 前缀
     - /novnc/vnc.html → /vnc.html
  6. 反向代理转发到 http://127.0.0.1:18001/vnc.html
```

#### 关键限制：代理需要 lease_id

**当前代理只支持有 lease_id 的 VM**（即 state=leased 的）。空闲 VM (state=ready) 没有 lease_id，无法通过子域名代理访问。

这是中控台需要解决的核心问题。

---

## 中控台需要做的事

### 方案：为每个 VM（含空闲的）提供 noVNC 访问

有两种思路，建议采用 **方案 A**（改后端）：

#### 方案 A：后端新增 vm_id 直连代理端点（推荐）

在 Worker 的 FastAPI server 上新增一个端点，允许通过 vm_id 直接访问 noVNC，不依赖 lease_id：

```
GET /v1/vms/<vm_id>/novnc/          → 反向代理到该 VM 的 noVNC
WS  /v1/vms/<vm_id>/novnc/websockify → WebSocket 代理
```

**实现位置**: `packages/server/src/cua_house_server/api/` 下新建路由文件，在 `app.py` 中注册（放在 catch-all proxy 之前）。

代理逻辑参考现有的 `proxy.py`：
- 通过 vm_id 查找 VMHandle 拿到 novnc_port
- 转发到 `http://127.0.0.1:{novnc_port}/`（去掉 `/v1/vms/{vm_id}/novnc` 前缀）
- 支持 HTTP（noVNC HTML/JS 资源）和 WebSocket（VNC 数据流）

然后中控台前端直接用：
```
http://<worker_ip>:8787/v1/vms/<vm_id>/novnc/
```

**优点**：不改 DNS / 防火墙；不依赖 lease_id；前端简单
**改动范围**：worker 端新增 1 个路由文件 + app.py 注册

#### 方案 B：前端通过 SSH 隧道（纯前端方案，不改后端）

中控台后端对每个要查看的 VM 建立 SSH 端口转发：

```
ssh -L <local_port>:127.0.0.1:<novnc_port> <worker_ip>
```

前端 iframe 指向 `http://localhost:<local_port>`。

**缺点**：需要管理 SSH 连接生命周期、端口分配、SSH key。

---

### 前端设计

#### 页面结构

```
┌─────────────────────────────────────────────────┐
│  CUA-House Dashboard                            │
├──────────────┬──────────────────────────────────┤
│  Workers     │                                  │
│  ┌────────┐  │   [noVNC Viewer]                 │
│  │ kvm02  │  │                                  │
│  │ ● VM-0 │◄─│   (选中 VM 后嵌入 noVNC 界面)     │
│  │ ○ VM-1 │  │                                  │
│  │ ○ VM-2 │  │                                  │
│  │ ...    │  │                                  │
│  ├────────┤  │                                  │
│  │ kvm03  │  │                                  │
│  │ ○ VM-0 │  │                                  │
│  │ ● VM-1 │  │                                  │
│  │ ...    │  │                                  │
│  └────────┘  │                                  │
│              │                                  │
│  ● leased    │                                  │
│  ○ ready     │                                  │
└──────────────┴──────────────────────────────────┘
```

#### noVNC 嵌入方式

noVNC 容器内部提供的是标准的 noVNC Web 界面：
- HTML 入口：`/vnc.html` 或 `/vnc_lite.html`
- WebSocket 端点：`/websockify`（VNC RFB 协议 over WebSocket）

两种嵌入方式：

**1. iframe 直接嵌入（简单）**
```html
<iframe src="http://<worker_ip>:8787/v1/vms/<vm_id>/novnc/vnc_lite.html" />
```

**2. 使用 @novnc/novnc npm 包（可定制）**
```javascript
import RFB from '@novnc/novnc/core/rfb';

const rfb = new RFB(
  document.getElementById('vnc-container'),
  `ws://<worker_ip>:8787/v1/vms/<vm_id>/novnc/websockify`
);
```

#### 数据刷新

定时轮询 `GET /v1/cluster/workers`（建议 5s 间隔）刷新 VM 列表和状态。

---

## 技术约束 & 注意事项

1. **GCP 防火墙只开了 8787**：所有访问必须走 Worker 的 :8787，不能直连 16000-18999 端口
2. **代理不需要 auth**：Worker 的 catch-all proxy 路由没有 auth 中间件（依赖 VPC 网络隔离）
3. **sslip.io DNS**：`*.IP.sslip.io` 自动解析到 IP，无需配置 DNS 记录
4. **Worker 的 `public_base_host`**：自动设为 `{external_ip}.sslip.io`，用于子域名代理的 Host header 匹配
5. **noVNC 的 /novnc 前缀**：现有代理对 service=novnc 会自动去掉 `/novnc` 前缀再转发，新端点也需要做同样的路径改写
6. **WebSocket 代理**：noVNC 的 VNC 数据流走 WebSocket（`/websockify`），代理必须同时支持 HTTP 和 WS 转发。现有实现在 `proxy.py` 的 `proxy_websocket_handler()` 中

## 相关源文件

| 文件 | 说明 |
|------|------|
| `packages/server/src/cua_house_server/api/proxy.py` | 现有子域名反向代理实现（HTTP + WebSocket） |
| `packages/server/src/cua_house_server/api/app.py` | FastAPI app 工厂 + catch-all 路由注册 |
| `packages/server/src/cua_house_server/api/routes.py` | task/batch API + URL 改写 (`_rewrite_assignment_urls`) |
| `packages/server/src/cua_house_server/api/cluster_routes.py` | `/v1/cluster/*` 监控 API |
| `packages/server/src/cua_house_server/runtimes/qemu.py` | VM 容器管理 + 端口分配（PortPool） |
| `packages/server/src/cua_house_server/scheduler/core.py` | Scheduler + `resolve_proxy_target()` |
| `packages/server/src/cua_house_server/cluster/worker_client.py` | Worker WS 客户端 + `_public_rewrite()` |
| `packages/server/src/cua_house_server/cluster/protocol.py` | 集群消息模型（WorkerVMSummary 等） |
| `packages/server/src/cua_house_server/config/loader.py` | 配置加载（public_base_host auto 逻辑） |

## 当前集群环境

- Master: `http://34.55.178.43:8787`
- Worker kvm02: `http://35.188.39.143:8787` (7 VMs, image=cpu-free, noVNC ports 18000-18006)
- Worker kvm03: `http://136.113.232.63:8787` (7 VMs, mixed images, noVNC ports 18000-18006)
