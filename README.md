# bettbox-mcp

让 AI agent 通过 [MCP](https://modelcontextprotocol.io) 控制本机正在运行的 [Bettbox](https://github.com/appshubcc/Bettbox)（基于 mihomo 内核的 Clash 客户端）。

**零客户端补丁。** 所有能力都来自 Bettbox 自己已经暴露的三个面：mihomo REST API、控制套接字、以及 `config.json` / `profiles/*.yaml`。

---

## 它能做什么

| 能力 | 工具 | 生效方式 |
|---|---|---|
| 查看整体状态 | `bettbox_get_status` | 只读 |
| 列出订阅（含配额/到期） | `bettbox_list_subscriptions` | 只读 |
| 列出策略组 / 组内节点 | `bettbox_list_proxy_groups` / `bettbox_list_proxies` | 只读 |
| 节点测速（单点或整组） | `bettbox_test_delay` | 只读 |
| 查看实时连接 | `bettbox_list_connections` | 只读 |
| **切换节点** | `bettbox_select_proxy` | 即时 |
| **出站模式**（规则/全局/直连） | `bettbox_set_mode` | 即时 |
| **TUN 开关** | `bettbox_set_tun` | 即时（仅本次运行） |
| **断开连接** | `bettbox_close_connections` | 即时 |
| **更新订阅内容** | `bettbox_update_subscription` | 写 YAML，下次应用生效 |
| **全部同步** | `bettbox_sync_all_subscriptions` | 同上，并发 |
| 添加 / 删除订阅 | `bettbox_add_subscription` / `bettbox_remove_subscription` | 需重启一次 |
| 重启 / 退出客户端 | `bettbox_restart_app` / `bettbox_stop_app` | 进程级 |

---

## 环境要求

- Python **3.10+**
- [uv](https://docs.astral.sh/uv/)（推荐）或 pip
- **Bettbox 桌面端已安装并运行**
- 在 Bettbox 里开启「外部控制器」（一次性，见下）

### 必做的一次性设置

打开 Bettbox → **设置 → 通用 → 外部控制器**。

开启后它会自动生成密钥并写入 `config.json`：

```json
"external-controller": "127.0.0.1:9090",
"secret": "<自动生成>"
```

验证：

```bash
curl -H "Authorization: Bearer <secret>" http://127.0.0.1:9090/version
# {"meta":true,"version":""}
```

> 密钥只存在于 `config.json` 的 `patchClashConfig.secret`，REST API **不会**回显它。本 MCP 从文件读取，写进日志与对话之前会脱敏。

---

## 安装

```bash
git clone https://github.com/InkiChang/bettbox-mcp.git
cd bettbox-mcp
uv sync
```

先跑一次体检（只读，不会碰 Bettbox）：

```bash
uv run python tests/platform_probe.py
```

输出结尾是 `RESULT: all checks passed` 就可以继续了。

---

## 在 DSH 里安装

### 方式一：图形界面

DSH → **设置 → MCP 连接器 → 添加自定义连接**，填：

| 字段 | 值 |
|---|---|
| 名称 | `Bettbox` |
| 传输方式 | `stdio` |
| 命令 | `/home/<你>/…/bettbox-mcp/.venv/bin/python` |
| 参数 | `-m bettbox_mcp` |
| 工作目录 | `…/bettbox-mcp` |
| 作用域 | 全局 |

保存后 DSH 会拉起服务，16 个工具以 `mcp__bettbox__*` 前缀注入。

> Linux 上**不要**用 `uv run` 作为命令——多一层进程会拖慢启动，也更容易被环境变量影响。直接用 venv 里的 `python`。

### 方式二：配置文件

DSH 把连接记在 `~/.dsh/storages/mcp_connector.json`。用图形界面添加更稳妥；若手改该文件，注意它由宿主维护、结构可能随版本变化。

### 方式三：给别的 MCP 客户端用

任何支持 MCP 的客户端都可以，标准配置：

```json
{
  "mcpServers": {
    "bettbox": {
      "command": "/home/<你>/bettbox-mcp/.venv/bin/python",
      "args": ["-m", "bettbox_mcp"],
      "cwd": "/home/<你>/bettbox-mcp"
    }
  }
}
```

Windows 上把命令换成 `...\bettbox-mcp\.venv\Scripts\python.exe`。

---

## 平台说明

### Linux（首选平台）

Linux 上控制通道走 **直接 Unix 域套接字连接**，不生成子进程、不依赖进程表，最稳。

| 安装方式 | 数据目录 | 备注 |
|---|---|---|
| deb / rpm / AUR | `~/.local/share/bettbox` | 二进制在 `PATH` 即可 |
| **AppImage** | `$XDG_DATA_HOME/bettbox` | 重启时用 `$APPIMAGE` 指向的真实文件，不是 `/tmp/.mount_*`；MCP 会自动补 `chmod +x` |
| snap | `~/snap/<name>/current/.local/share/…` | — |
| flatpak | `~/.var/app/<id>/data/…` | — |

**TUN 权限**：需要 core 具备 `cap_net_admin`。

```bash
sudo setcap cap_net_admin,cap_net_bind_service,cap_net_raw=+ep <core 路径>
```

Bettbox 在你第一次开 TUN 时会尝试自动完成（失败回退 setuid）。**本 MCP 不接触、不保存任何密码**，只做状态探测。

**Wayland**：启动 GUI 需要 `WAYLAND_DISPLAY` 或 `DISPLAY`，headless 环境下 `bettbox_restart_app` 会明确报错而不是静默失败。

### macOS

路径 `/Applications/Bettbox.app/Contents/MacOS/Bettbox`，控制通道同样是 UDS。首次开 TUN 需在系统弹窗里授权一次。

### Windows

> ⚠️ **已知限制**：CPython 在 Windows 上没有 `socket.AF_UNIX`，控制命令只能通过 `Bettbox.exe --exit` / `--restart` 生成子进程转发。如果 MCP 运行在**限制了进程可见性**的沙箱里（实测 DSH 就是这样：外部 shell 能看到 383 个进程，被沙箱的 Python 只看到 55 个），子进程会报 `Bad state: Bettbox is not running`，**启停类操作不可用**。

| 能力 | 沙箱内 | 沙箱外 |
|---|---|---|
| 只读查询 / Clash API 热写 / 更新订阅 YAML | ✅ | ✅ |
| 增删订阅、启停 App | ❌ | ✅ |

只读与热写不受影响，日常使用足够；需要增删订阅时手动操作客户端即可。

---

## 工作原理

```
┌──────────────┐
│  MCP client  │  (DSH / Claude Desktop / Cursor …)
└──────┬───────┘
       │ stdio (JSON-RPC)
┌──────▼───────────────────────────────────────┐
│  bettbox-mcp                                 │
│  ├─ ClashClient  ──HTTP──▶ 127.0.0.1:9090    │  运行时读写，即时生效
│  ├─ app_control  ──UDS──▶  Bettbox.control.sock │  启停/重启
│  └─ store        ──file──▶ config.json, profiles/*.yaml │  订阅增删改
└──────────────────────────────────────────────┘
```

### 为什么有些操作要重启

Bettbox 在**优雅退出时**会把内存里的 `Config` 写回 `config.json`（`savePreferences()`）。所以 App 运行期间改这个文件会被覆盖。

- 订阅列表、应用设置 → 存在 `config.json` → **必须「退出 → 改 → 启动」**
- 订阅内容 → 存在 `profiles/*.yaml` → **退出时不重写** → 无需重启

### 订阅更新的实现

「更新订阅」是复刻了 App 里「全部同步」按钮的逻辑（`Profile.update()` → `saveFile()`）：

1. 用 App 自己的 UA（`FlClash/ClashMetaForAndroid/…`）**经代理**下载 ——服务商按 UA 决定返回 Clash YAML 还是节点列表
2. `utf8.decode` → tab 转两个空格（同 `patchYamlConfig`）
3. 结构校验（替代 App 内部的 FFI 校验，见下）
4. 内容相同则跳过；否则备份后原子写（temp + rename）
5. 可选：回写 `lastUpdateDate` 与 `subscriptionInfo`

**唯一无法复刻的部分**：App 用 `clashCore.validateConfig`（Dart↔Go 的 FFI）做完整校验，没有 REST 出口。本 MCP 用轻量结构校验替代（必须含 `proxies`/`proxy-groups`、必须含端口键），能拦住空响应、HTML 错误页、被截断的 YAML。

---

## 安全边界

- **密钥不出文件**：只从 `config.json` 读，不写日志、不进 tool 返回值
- **订阅 URL 默认打码**（多数 URL 内含账号 token），需要时用 `reveal_urls=true`
- **所有写操作先备份**：`config.json.mcp-bak-<时间戳>`、`<id>.yaml.mcp-bak`
- **不提供「修改内核路径」类工具**：Bettbox 的 core 带提权位（setuid/setcap），暴露它就等于开放提权链
- **拒绝删除当前正在使用的订阅**，避免自断网络

---

## 开发

```bash
uv run python -m bettbox_mcp                # 以 stdio 启动

uv run python tests/platform_probe.py       # 环境体检（只读）
uv run python tests/platform_probe.py --apply   # 含启停验证（会短暂断网）
uv run python tests/smoke.py                # 只读工具回归
uv run python tests/write_tools.py          # 可逆写操作回归
uv run python tests/stdio_protocol.py       # 真实 MCP 客户端握手
uv run python tests/agent_workflow.py       # 复合任务：测速→选最优→切换→验证→还原
```

### 项目结构

```
src/bettbox_mcp/
├── config.py       跨平台数据目录 / 可执行文件发现，形状校验
├── clash_api.py    mihomo REST 客户端（httpx, trust_env=False 直连）
├── app_control.py  进程控制：UDS / CLI 双通道，RestartWindow
├── store.py        config.json 原子读写，订阅同步逻辑
├── models.py       Pydantic 输入校验
└── server.py       FastMCP 工具注册
```

---

## 实测记录

Bettbox **1.19.7** / Windows 11 / Python 3.11：

- 外部控制器开启后 9090 正常监听，无密钥访问返回 **401**
- `PATCH /configs` 改 `mode` / `log-level` 即时生效
- `PUT /proxies/{group}` 切节点即时生效，`/proxies` 立即可见
- `GET /proxies/{name}/delay` 返回真实延迟
- 订阅增删：`config.json` 的 `profiles` 数组可安全增删，App 启动不回滚
- 订阅更新：复刻「全部同步」成功，YAML 与配额均被 App 正确读回
- `GET /configs` **不含** `external-controller` / `secret`（只能从文件读）
- `/traffic` 是 WebSocket 端点，普通 GET 会挂起
- `--quick` / `--boot` 是 **Android 专属**，桌面端只认 `--exit` / `--restart`

Linux / macOS 的路径发现与 UDS 通道已实现，但**尚未在真机验证**——用 `tests/platform_probe.py` 确认后再接线。

---

## 致谢

- [Bettbox](https://github.com/appshubcc/Bettbox) — FlClash 分支，本项目所有能力都建立在它已有的接口之上
- [mihomo](https://github.com/MetaCubeX/mihomo) — 内核
- [mcp-builder](https://github.com/modelcontextprotocol) — 服务端设计参考

## License

[MIT](LICENSE)
