# Kimi Usage Board

一个多人共享的 **Kimi Coding Plan 额度看板**网站：同时监控两个 API Key 的每周用量与 5 小时限额，每 120 秒自动刷新，支持手动刷新。

设计参考自 [kimi-code-usage](../kimi-code-usage)，上游数据解析逻辑移植自其 `providers/kimi.py`。

## 快速开始

```bash
cd kimi-usage-board
cp .env.example .env   # 填入 KIMI_KEY_1 / KIMI_KEY_2 … 和 BOARD_PASSWORD
uv run kimi-board
```

打开 http://127.0.0.1:8080 （局域网内其他设备访问启动时打印的局域网地址）。

## 日常运维：restart.sh

修改 `.env`（增删 Key、改密码、改端口）后，运行脚本即可让配置生效：

```bash
./restart.sh
```

脚本会自动：停止旧进程（按 `.env` 中的端口）→ 后台启动新进程（`nohup`，日志写入 `server.log`，关闭终端不影响运行）→ 等待服务就绪 → 打印本机与局域网访问地址。

```bash
tail -f server.log                  # 查看运行日志
lsof -ti :8080 | xargs kill         # 彻底停止服务
```

> 注意：服务重启后所有登录态失效（签名密钥每次启动重新生成），需重新输入密码。

## 多人访问与并发设计

| 机制 | 作用 |
|:---|:---|
| **Key 不下发前端** | Key 只存服务端 `.env`，接口只返回掩码尾号（`···zhDP`），多人访问安全 |
| **共享缓存（TTL 110s）** | N 个访客在缓存期内共享同一份数据，不重复请求上游 |
| **Single-flight 锁** | 缓存失效瞬间的并发请求经 `asyncio.Lock` 合并为**一次**上游调用 |
| **手动刷新限频（15s）** | 全局最小间隔，防止访客狂点按钮打爆上游；被限频时返回缓存并提示 |
| **失败保留旧数据** | 上游故障时返回 stale 数据 + 错误标记，页面不空白 |

## 访问鉴权

- 访问数据接口需先输入密码（`.env` 中的 `BOARD_PASSWORD`，置空则关闭鉴权，仅限本机调试）；
- 登录成功后签发 **HMAC 签名 Cookie**（HttpOnly，7 天有效），签名密钥每次启动随机生成 —— 服务重启后所有登录态自动失效；
- 防爆破：同一 IP 连续输错 5 次锁定 60 秒；
- 密码与 API Key 一样只存在于服务端，前端不存储。

## 配置（.env）

```bash
# Key 数量不限：按序号追加即可（KIMI_KEY_3、KIMI_KEY_4 …），
# 重启服务后面板自动出现新卡片
KIMI_KEY_1=sk-kimi-xxx
KIMI_KEY_2=sk-kimi-yyy
BOARD_PASSWORD=your_password  # 访问密码
# KIMI_BASE_URL=https://api.kimi.com/coding/v1
# HOST=0.0.0.0
# PORT=8080
```
