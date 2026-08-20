"""Kimi Usage Board — 多人共享缓存的 Kimi Coding Plan 额度看板。

并发设计：
- 每个 Key 一把 asyncio.Lock（single-flight）：缓存失效时，并发请求合并为一次上游调用；
- 共享缓存 TTL 110s：N 个访客在 TTL 内共享同一份数据，不重复打上游 API；
- 手动刷新全局限频 15s：防止访客狂点按钮打爆上游；
- 上游失败时保留旧数据（stale 标记），页面不空白。
- API Key 只存在于服务端 .env，接口只返回掩码尾号。
"""

import asyncio
import hashlib
import hmac
import os
import secrets
import socket
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

STATIC_DIR = Path(__file__).parent / "static"

CACHE_TTL = 110.0           # 秒；前端每 120s 轮询一次，TTL 略小于轮询周期
FORCE_MIN_INTERVAL = 15.0   # 手动刷新全局最小间隔（秒）
UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(total=10)
USER_AGENT = "KimiCLI/1.6"


# ---------- 上游响应解析（移植自 kimi-code-usage 的 providers/kimi.py） ----------

def _to_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_reset(data: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """返回 (展示用 "MM-DD HH:MM", ISO 时间戳)，前端用 ISO 做实时倒计时。"""
    reset_at = data.get("resetTime") or data.get("reset_at") or data.get("reset_time")
    if reset_at:
        try:
            if isinstance(reset_at, (int, float)):
                dt = datetime.fromtimestamp(reset_at).astimezone()
            else:
                dt = datetime.fromisoformat(str(reset_at).replace("Z", "+00:00")).astimezone()
            return dt.strftime("%m-%d %H:%M"), dt.isoformat()
        except Exception:
            pass
    reset_in = _to_int(data.get("reset_in"))
    if reset_in is not None:
        dt = datetime.now().astimezone() + timedelta(seconds=reset_in)
        return dt.strftime("%m-%d %H:%M"), dt.isoformat()
    return None, None


def _limit_label(window: Mapping[str, Any], idx: int) -> str:
    duration = _to_int(window.get("duration"))
    time_unit = str(window.get("timeUnit") or window.get("time_unit") or "").upper()
    if duration is not None:
        if "MINUTE" in time_unit:
            if duration >= 60 and duration % 60 == 0:
                return f"{duration // 60}h Limit"
            return f"{duration}m Limit"
        if "HOUR" in time_unit:
            return f"{duration}h Limit"
        if "DAY" in time_unit:
            return f"{duration}d Limit"
        if "MONTH" in time_unit:
            return f"{duration}mo Limit"
        return f"{duration}s Limit"
    return f"Limit #{idx + 1}"


def _to_row(data: Mapping[str, Any], default_label: str) -> dict[str, Any] | None:
    limit = _to_int(data.get("limit") or data.get("limit_amount"))
    used = _to_int(data.get("used") or data.get("used_amount"))
    if used is None:
        remaining = _to_int(data.get("remaining"))
        if remaining is not None and limit is not None:
            used = limit - remaining
    if used is None and limit is None:
        return None
    used = used or 0
    limit = limit or 0
    reset_display, reset_iso = _parse_reset(data)
    return {
        "label": str(data.get("name") or data.get("title") or data.get("model_name") or default_label),
        "used": used,
        "limit": limit,
        "percent": round(used / limit * 100, 1) if limit > 0 else 0.0,
        "reset_display": reset_display,
        "reset_iso": reset_iso,
    }


def parse_usage_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """与参考项目一致：优先解析 data 列表形态，否则解析 usage/limits 形态。"""
    rows: list[dict[str, Any]] = []

    data_list = payload.get("data")
    if isinstance(data_list, Sequence) and not isinstance(data_list, (str, bytes)):
        summary = None
        limits = []
        for item in data_list:
            if not isinstance(item, Mapping):
                continue
            row = _to_row(item, "Weekly Usage" if item.get("model_name") == "all" else "Limit")
            if row:
                if item.get("model_name") == "all":
                    summary = row
                else:
                    limits.append(row)
        rows = ([summary] if summary else []) + limits
    else:
        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            row = _to_row(cast(Mapping, usage), "Weekly Usage")
            if row:
                rows.append(row)
        raw_limits = payload.get("limits")
        if isinstance(raw_limits, Sequence) and not isinstance(raw_limits, (str, bytes)):
            for idx, item in enumerate(raw_limits):
                if not isinstance(item, Mapping):
                    continue
                detail = item.get("detail") if isinstance(item.get("detail"), Mapping) else item
                window = item.get("window") if isinstance(item.get("window"), Mapping) else {}
                row = _to_row(cast(Mapping, detail), _limit_label(cast(Mapping, window), idx))
                if row:
                    rows.append(row)
    return rows


def _error_hint(status: int) -> str:
    hints = {
        401: "API 认证失败（401），请检查 API Key 是否为 Kimi Coding Plan 的 sk-kimi-xxx",
        403: "API 拒绝访问（403），请检查 Key 权限",
        404: "用量接口不存在（404），请检查 KIMI_BASE_URL",
        429: "上游限流（429），稍后自动恢复",
    }
    return hints.get(status, f"Kimi API 返回错误 {status}")


async def _fetch_upstream(session: aiohttp.ClientSession, api_key: str, base_url: str) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT}
    base = base_url.rstrip("/")
    async with session.get(f"{base}/usages", headers=headers, timeout=UPSTREAM_TIMEOUT) as resp:
        if resp.status == 200:
            payload = await resp.json()
        elif resp.status == 404:
            async with session.get(f"{base}/usage", headers=headers, timeout=UPSTREAM_TIMEOUT) as r2:
                if r2.status != 200:
                    raise RuntimeError(_error_hint(r2.status))
                payload = await r2.json()
        else:
            raise RuntimeError(_error_hint(resp.status))
    rows = parse_usage_payload(payload)
    if not rows:
        raise RuntimeError("上游返回了空用量数据")
    return rows


# ---------- 每 Key 的共享缓存 + single-flight 锁 ----------

class KeyUsageCache:
    def __init__(self, key_id: str, name: str, api_key: str, base_url: str):
        self.key_id = key_id
        self.name = name
        self.api_key = api_key
        self.base_url = base_url
        self._lock = asyncio.Lock()
        self._rows: list[dict[str, Any]] | None = None
        self._error: str | None = None
        self._fetched_mono = 0.0      # monotonic 时间，用于 TTL 判断
        self._fetched_iso: str | None = None

    async def get(self, session: aiohttp.ClientSession, force: bool = False) -> dict[str, Any]:
        # 锁内做双重检查：并发请求在锁外等待，拿到锁后发现缓存已新鲜则直接返回，
        # 保证缓存失效瞬间的 N 个并发请求只触发一次上游调用。
        async with self._lock:
            now = time.monotonic()
            has_data = self._rows is not None
            age = now - self._fetched_mono

            if has_data and age < CACHE_TTL and not force:
                return self._snapshot()

            # 手动刷新限频：距离上次上游调用不足 FORCE_MIN_INTERVAL，直接返回现有数据
            if force and has_data and age < FORCE_MIN_INTERVAL:
                snap = self._snapshot()
                snap["rate_limited"] = True
                return snap

            await self._fetch(session)
            return self._snapshot()

    async def _fetch(self, session: aiohttp.ClientSession) -> None:
        # 无论成败都更新 _fetched_mono：失败同样限频，避免上游故障时被打爆
        self._fetched_mono = time.monotonic()
        self._fetched_iso = datetime.now().astimezone().isoformat()
        try:
            self._rows = await _fetch_upstream(session, self.api_key, self.base_url)
            self._error = None
        except Exception as exc:
            # 保留旧数据，只记录错误；首次启动失败时 _rows 为 None，前端显示错误态
            self._error = str(exc) or type(exc).__name__

    def _snapshot(self) -> dict[str, Any]:
        stale = self._rows is not None and (time.monotonic() - self._fetched_mono) >= CACHE_TTL
        return {
            "id": self.key_id,
            "name": self.name,
            "tail": f"···{self.api_key[-4:]}",
            "ok": self._error is None,
            "error": self._error,
            "stale": stale,
            "fetched_at": self._fetched_iso,
            "rows": self._rows or [],
        }


# ---------- 访问鉴权（密码 + HMAC 签名 Cookie） ----------

AUTH_COOKIE = "kimi_board_auth"
SESSION_TTL = 7 * 24 * 3600   # Cookie 有效期 7 天
LOGIN_MAX_FAILS = 5           # 同一 IP 连续失败次数上限
LOGIN_LOCKOUT = 60.0          # 触发上限后锁定秒数


class AuthManager:
    """密码校验 + 签名令牌。密钥每次启动随机生成，重启后旧 Cookie 自动失效。"""

    def __init__(self, password: str):
        self._password = password.encode()
        self._secret = secrets.token_bytes(32)
        self._fail_lock = asyncio.Lock()
        self._fails: dict[str, list[float]] = {}  # ip -> [失败次数, 锁定截止 monotonic]

    def make_token(self) -> str:
        exp = str(int(time.time()) + SESSION_TTL)
        sig = hmac.new(self._secret, exp.encode(), hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def check_token(self, token: str) -> bool:
        try:
            exp, sig = token.split(".", 1)
        except ValueError:
            return False
        expected = hmac.new(self._secret, exp.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        return exp.isdigit() and int(exp) > time.time()

    async def verify_password(self, ip: str, password: str) -> tuple[bool, str | None]:
        """返回 (是否通过, 错误提示)。带防爆破：同一 IP 连错 5 次锁定 60 秒。"""
        async with self._fail_lock:
            count, lockout_until = self._fails.get(ip, [0, 0.0])
            wait = lockout_until - time.monotonic()
            if wait > 0:
                return False, f"尝试过于频繁，请 {int(wait) + 1} 秒后再试"

        ok = hmac.compare_digest(password.encode(), self._password)

        async with self._fail_lock:
            if ok:
                self._fails.pop(ip, None)
            else:
                count += 1
                lockout = time.monotonic() + LOGIN_LOCKOUT if count >= LOGIN_MAX_FAILS else 0.0
                self._fails[ip] = [count, lockout]
        if ok:
            return True, None
        remaining = LOGIN_MAX_FAILS - count
        if remaining > 0:
            return False, f"密码错误，还可尝试 {remaining} 次"
        return False, f"失败次数过多，已锁定 {int(LOGIN_LOCKOUT)} 秒"


def _authorized(request: web.Request) -> bool:
    auth: AuthManager | None = request.app["auth"]
    if auth is None:
        return True
    return auth.check_token(request.cookies.get(AUTH_COOKIE, ""))


async def handle_login(request: web.Request) -> web.Response:
    auth: AuthManager | None = request.app["auth"]
    if auth is None:
        return web.json_response({"ok": True})
    try:
        body = await request.json()
        password = str(body.get("password", ""))
    except Exception:
        return web.json_response({"ok": False, "error": "请求格式错误"}, status=400)

    ok, error = await auth.verify_password(request.remote or "unknown", password)
    if not ok:
        return web.json_response({"ok": False, "error": error}, status=401)

    resp = web.json_response({"ok": True})
    resp.set_cookie(
        AUTH_COOKIE, auth.make_token(),
        max_age=SESSION_TTL, httponly=True, samesite="Lax",
    )
    return resp


async def handle_logout(request: web.Request) -> web.Response:
    resp = web.json_response({"ok": True})
    resp.del_cookie(AUTH_COOKIE)
    return resp


# ---------- HTTP 处理器 ----------

async def handle_index(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC_DIR / "index.html")


async def _collect(request: web.Request, force: bool) -> web.Response:
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    session: aiohttp.ClientSession = request.app["http"]
    caches: list[KeyUsageCache] = request.app["caches"]
    # 两个 Key 并发拉取；每个 Key 内部由各自的锁做 single-flight
    results = await asyncio.gather(*(c.get(session, force=force) for c in caches))
    return web.json_response({
        "server_time": datetime.now().astimezone().isoformat(),
        "keys": results,
    })


async def handle_usage(request: web.Request) -> web.Response:
    return await _collect(request, force=False)


async def handle_refresh(request: web.Request) -> web.Response:
    return await _collect(request, force=True)


async def _on_startup(app: web.Application) -> None:
    app["http"] = aiohttp.ClientSession()


async def _on_cleanup(app: web.Application) -> None:
    await app["http"].close()


def _load_key_caches(base_url: str) -> list[KeyUsageCache]:
    """扫描环境变量中的 KIMI_KEY_<数字>，支持任意数量的 Key。

    新增号只需在 .env 里追加一行（如 KIMI_KEY_3=sk-kimi-xxx）并重启服务，
    面板会自动出现对应卡片。
    """
    numbered: list[tuple[int, str]] = []
    for name, value in os.environ.items():
        if not name.startswith("KIMI_KEY_"):
            continue
        suffix = name[len("KIMI_KEY_"):]
        if suffix.isdigit() and value.strip():
            numbered.append((int(suffix), value.strip()))
    numbered.sort()
    return [
        KeyUsageCache(f"key{n}", f"Key {n}", key, base_url)
        for n, key in numbered
    ]


def create_app() -> web.Application:
    load_dotenv()
    base_url = os.getenv("KIMI_BASE_URL", "https://api.kimi.com/coding/v1")

    caches = _load_key_caches(base_url)
    if not caches:
        raise SystemExit("未配置任何 Key：请在 .env 中设置 KIMI_KEY_1、KIMI_KEY_2 …")

    app = web.Application()
    app["caches"] = caches
    # BOARD_PASSWORD 为空则不启用鉴权（仅限本机调试）
    password = os.getenv("BOARD_PASSWORD", "").strip()
    app["auth"] = AuthManager(password) if password else None
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/usage", handle_usage)
    app.router.add_post("/api/refresh", handle_refresh)
    app.router.add_post("/api/login", handle_login)
    app.router.add_post("/api/logout", handle_logout)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.1)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def run() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    app = create_app()

    def _banner(_app: web.Application) -> None:
        print("🌕 Kimi Usage Board")
        print(f"   本机访问:  http://127.0.0.1:{port}")
        print(f"   局域网访问: http://{_lan_ip()}:{port}")

    web.run_app(app, host=host, port=port, print=_banner)


if __name__ == "__main__":
    run()
