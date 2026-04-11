"""
Admin 后台路由 — 提供管理界面的 API 端点。
- Cookie/Session 认证保护（页面登录）
- 系统状态仪表盘
- 模型管理（查看、启用/禁用、刷新）
- 日志查看（实时、按文件）
- 配置管理（查看、热更新）
"""
import json
import logging
import secrets
import hashlib
import hmac
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query, Request, Depends, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from config import settings
from model_manager import model_manager
from stats import stats_collector

logger = logging.getLogger("admin")

router = APIRouter(prefix="/admin", tags=["admin"])

# ── Cookie/Session 认证 ─────────────────────────────────
# 用 HMAC 签名的 cookie 做轻量 session，无需服务端存储

_SESSION_COOKIE = "msp_session"
_SESSION_MAX_AGE = 86400 * 7  # 7 天有效


def _sign_session(username: str, expires: int) -> str:
    """生成 session 签名"""
    msg = f"{username}:{expires}"
    sig = hmac.new(
        settings.admin_password.encode(), msg.encode(), hashlib.sha256
    ).hexdigest()
    return f"{msg}:{sig}"


def _verify_session(token: str) -> Optional[str]:
    """验证 session token，返回用户名或 None"""
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return None
        username, expires_str, sig = parts
        expires = int(expires_str)

        # 检查过期
        if time.time() > expires:
            return None

        # 验证签名
        expected = _sign_session(username, expires)
        if not hmac.compare_digest(token, expected):
            return None

        # 验证用户名
        if not secrets.compare_digest(username.encode(), settings.admin_username.encode()):
            return None

        return username
    except Exception:
        return None


def _get_session_from_request(request: Request) -> Optional[str]:
    """从请求中获取 session 用户名"""
    token = request.cookies.get(_SESSION_COOKIE)
    if not token:
        return None
    return _verify_session(token)


def require_auth(request: Request) -> str:
    """认证依赖：验证 cookie session，失败返回 401"""
    username = _get_session_from_request(request)
    if not username:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return username


# ── 登录页面 ──────────────────────────────────────────
_LOGIN_HTML: Optional[str] = None


def _load_login_html() -> str:
    """加载并缓存 login.html"""
    global _LOGIN_HTML
    if _LOGIN_HTML is None:
        html_path = Path(__file__).parent / "login.html"
        _LOGIN_HTML = html_path.read_text(encoding="utf-8")
    return _LOGIN_HTML


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    """登录页面"""
    return HTMLResponse(content=_load_login_html())


@router.post("/api/login")
async def api_login(request: Request):
    """登录 API"""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效请求")

    username = data.get("username", "")
    password = data.get("password", "")

    correct_username = secrets.compare_digest(username.encode(), settings.admin_username.encode())
    correct_password = secrets.compare_digest(password.encode(), settings.admin_password.encode())

    if not (correct_username and correct_password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 生成 session
    expires = int(time.time()) + _SESSION_MAX_AGE
    token = _sign_session(username, expires)

    response = JSONResponse(content={"message": "登录成功", "username": username})
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    logger.info(f"管理员登录成功: {username}")
    return response


@router.post("/api/logout")
async def api_logout():
    """退出登录"""
    response = JSONResponse(content={"message": "已退出登录"})
    response.delete_cookie(key=_SESSION_COOKIE)
    return response


# ── 前端页面 ──────────────────────────────────────────
_ADMIN_HTML: Optional[str] = None


def _load_admin_html() -> str:
    """加载并缓存 admin.html"""
    global _ADMIN_HTML
    if _ADMIN_HTML is None:
        html_path = Path(__file__).parent / "admin.html"
        _ADMIN_HTML = html_path.read_text(encoding="utf-8")
    return _ADMIN_HTML


@router.get("", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Admin 管理后台页面（需要登录）"""
    username = _get_session_from_request(request)
    if not username:
        return RedirectResponse(url="/admin/login", status_code=302)
    return HTMLResponse(content=_load_admin_html())


# ── 系统状态 ──────────────────────────────────────────
@router.get("/api/status")
async def system_status(username: str = Depends(require_auth)):
    """获取系统状态概览（需要认证）"""
    status = model_manager.get_status()
    return JSONResponse(content={
        "service": "ModelScope API 转换器",
        "version": "1.0.0",
        "uptime": _get_uptime(),
        "virtual_model": settings.virtual_model_name,
        "proxy_port": settings.proxy_port,
        "model_stats": {
            "total": status["total"],
            "active": status["active"],
            "disabled_today": status["disabled_today"],
            "cooldown_count": status.get("cooldown_count", 0),
            "current_model": status.get("current_model"),
        },
        "refresh_interval": settings.model_refresh_interval,
        "min_param_b": settings.min_param_b,
        "last_refresh": _get_last_refresh_time(),
    })


def _get_uptime() -> str:
    """获取服务运行时间"""
    try:
        data_file = settings.data_dir / "start_time.json"
        if data_file.exists():
            data = json.loads(data_file.read_text(encoding="utf-8"))
            start = datetime.fromisoformat(data["start_time"])
            delta = datetime.now() - start
            hours, remainder = divmod(int(delta.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            return f"{hours}h {minutes}m {seconds}s"
    except Exception:
        pass
    return "unknown"


def _get_last_refresh_time() -> str:
    """获取上次刷新时间"""
    try:
        cache_file = settings.data_dir / "model_cache.json"
        if cache_file.exists():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            return data.get("updated_at", "unknown")
    except Exception:
        pass
    return "unknown"


# ── 模型管理 ──────────────────────────────────────────
@router.get("/api/models")
async def list_models(username: str = Depends(require_auth)):
    """获取模型列表（含状态，需要认证）"""
    status = model_manager.get_status()
    return JSONResponse(content=status)


@router.post("/api/models/enable")
async def enable_model(data: dict, username: str = Depends(require_auth)):
    """手动启用一个被禁用的模型（需要认证）"""
    model_id = data.get("model_id", "")
    if not model_id:
        return JSONResponse(content={"error": "model_id is required"}, status_code=400)
    with model_manager._lock:
        if model_id in model_manager._disabled:
            del model_manager._disabled[model_id]
            logger.info(f"管理员手动启用模型: {model_id}")
            return JSONResponse(content={"message": f"模型 {model_id} 已启用"})
    return JSONResponse(content={"message": f"模型 {model_id} 未被禁用"}, status_code=200)


@router.post("/api/models/disable")
async def disable_model(data: dict, username: str = Depends(require_auth)):
    """手动禁用一个模型（需要认证）"""
    model_id = data.get("model_id", "")
    if not model_id:
        return JSONResponse(content={"error": "model_id is required"}, status_code=400)
    model_manager.mark_disabled(model_id, "管理员手动禁用")
    return JSONResponse(content={"message": f"模型 {model_id} 已禁用"})


@router.post("/api/refresh")
async def refresh_models(username: str = Depends(require_auth)):
    """手动触发模型列表刷新（需要认证）"""
    model_manager.refresh_models()
    return JSONResponse(content={
        "message": "模型列表已刷新",
        "timestamp": datetime.now().isoformat(),
    })


@router.post("/api/reset-disabled")
async def reset_all_disabled(username: str = Depends(require_auth)):
    """重置所有被禁用的模型（需要认证）"""
    with model_manager._lock:
        count = len(model_manager._disabled)
        model_manager._disabled.clear()
        model_manager._current_index = 0
    logger.info(f"管理员重置了 {count} 个被禁用的模型")
    return JSONResponse(content={"message": f"已重置 {count} 个被禁用的模型"})


# ── 日志查看 ──────────────────────────────────────────
@router.get("/api/logs")
async def get_logs(
    lines: int = Query(default=200, ge=1, le=2000),
    level: str = Query(default="ALL", description="过滤级别: ALL, DEBUG, INFO, WARNING, ERROR"),
    search: str = Query(default="", description="搜索关键词"),
    username: str = Depends(require_auth),
):
    """获取日志内容（需要认证）"""
    log_file = settings.log_dir / "modelscope-proxy.log"
    if not log_file.exists():
        return JSONResponse(content={"logs": [], "total": 0, "file": str(log_file)})

    try:
        all_lines = log_file.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

    # 过滤
    filtered = []
    level_upper = level.upper()
    for line in all_lines:
        if level_upper != "ALL" and f"[{level_upper}]" not in line:
            continue
        if search and search.lower() not in line.lower():
            continue
        filtered.append(line)

    # 取最后 N 行
    result_lines = filtered[-lines:]

    return JSONResponse(content={
        "logs": result_lines,
        "total": len(filtered),
        "showing": len(result_lines),
        "file": str(log_file),
    })


@router.get("/api/logs/files")
async def list_log_files(username: str = Depends(require_auth)):
    """列出所有日志文件（需要认证）"""
    log_dir = settings.log_dir
    files = []
    if log_dir.exists():
        for f in sorted(log_dir.glob("modelscope-proxy.log*"), reverse=True):
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
    return JSONResponse(content={"files": files})


@router.get("/api/logs/download/{filename}")
async def download_log(filename: str, username: str = Depends(require_auth)):
    """下载日志文件（需要认证）"""
    # 安全检查：防止路径遍历
    if ".." in filename or "/" in filename or "\\" in filename:
        return JSONResponse(content={"error": "非法文件名"}, status_code=400)

    log_file = settings.log_dir / filename
    if not log_file.exists():
        return JSONResponse(content={"error": "文件不存在"}, status_code=404)

    content = log_file.read_text(encoding="utf-8")
    return PlainTextResponse(content=content, headers={
        "Content-Disposition": f"attachment; filename={filename}"
    })


# ── 密码修改 ──────────────────────────────────────────
@router.post("/api/change-password")
async def change_password(data: dict, username: str = Depends(require_auth)):
    """修改管理后台密码（需要认证）"""
    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")

    if not current_password or not new_password:
        return JSONResponse(content={"error": "请填写当前密码和新密码"}, status_code=400)

    if not secrets.compare_digest(current_password.encode(), settings.admin_password.encode()):
        return JSONResponse(content={"error": "当前密码不正确"}, status_code=400)

    if len(new_password) < 6:
        return JSONResponse(content={"error": "新密码长度至少 6 位"}, status_code=400)

    old_preview = f"****{settings.admin_password[-4:]}" if len(settings.admin_password) >= 8 else "****"
    settings.admin_password = new_password

    # 保存到 .env
    _save_to_env({"admin_password": {"old": old_preview, "new": new_password}})

    logger.info(f"管理员 {username} 修改了后台密码")
    return JSONResponse(content={"message": "密码修改成功，下次登录请使用新密码"})


# ── 配置管理 ──────────────────────────────────────────
@router.get("/api/config")
async def get_config(username: str = Depends(require_auth)):
    """获取当前配置（隐藏敏感信息，需要认证）"""
    return JSONResponse(content={
        "modelscope_base_url": settings.modelscope_base_url,
        "proxy_port": settings.proxy_port,
        "virtual_model_name": settings.virtual_model_name,
        "min_param_b": settings.min_param_b,
        "model_refresh_interval": settings.model_refresh_interval,
        "log_level": settings.log_level,
        "log_retention_days": settings.log_retention_days,
        "show_model_tag": settings.show_model_tag,
        "api_key_set": bool(settings.modelscope_api_key),
        "api_key_preview": f"****{settings.modelscope_api_key[-4:]}" if len(settings.modelscope_api_key) >= 8 else "****",
        "admin_username": settings.admin_username,
    })


@router.post("/api/config")
async def update_config(config: dict, username: str = Depends(require_auth)):
    """热更新配置（部分字段支持运行时修改，需要认证）"""
    updatable_fields = {
        "min_param_b": int,
        "model_refresh_interval": int,
        "log_level": str,
        "virtual_model_name": str,
        "modelscope_api_key": str,
        "log_retention_days": int,
        "show_model_tag": bool,
    }

    updated = {}
    for field, type_fn in updatable_fields.items():
        if field in config:
            try:
                new_value = type_fn(config[field])
                old_value = getattr(settings, field)

                # API Key 特殊处理：不显示旧值，且不能为空
                if field == "modelscope_api_key":
                    if not new_value or not new_value.startswith("ms-"):
                        return JSONResponse(
                            content={"error": "API Key 格式无效，需以 ms- 开头"},
                            status_code=400,
                        )

                # log_retention_days 必须 >= 0
                if field == "log_retention_days" and new_value < 0:
                    return JSONResponse(
                        content={"error": "日志保留天数不能为负数，0 表示永不清空"},
                        status_code=400,
                    )

                if new_value != old_value:
                    setattr(settings, field, new_value)
                    # API Key 更新时隐藏旧值
                    if field == "modelscope_api_key":
                        updated[field] = {"old": "****", "new": f"****{new_value[-4:]}"}
                    else:
                        updated[field] = {"old": old_value, "new": new_value}

                    # 特殊处理日志级别
                    if field == "log_level":
                        root_logger = logging.getLogger()
                        root_logger.setLevel(getattr(logging, new_value.upper(), logging.INFO))

            except (ValueError, TypeError) as e:
                return JSONResponse(
                    content={"error": f"字段 {field} 值无效: {e}"},
                    status_code=400,
                )

    # 如果修改了 min_param_b 或 model_refresh_interval，保存到 .env
    if updated:
        _save_to_env(updated)
        logger.info(f"配置已更新: {updated}")

        # 如果修改了 min_param_b 或 modelscope_api_key，触发模型刷新
        if "min_param_b" in updated or "modelscope_api_key" in updated:
            model_manager.refresh_models()

    return JSONResponse(content={
        "message": "配置已更新",
        "updated": updated,
    })


def _save_to_env(changes: dict):
    """将变更的配置保存到 .env 文件"""
    env_path = Path(__file__).parent / ".env"

    # 读取现有 .env
    env_lines = []
    if env_path.exists():
        env_lines = env_path.read_text(encoding="utf-8").splitlines()

    env_keys = {}
    for line in env_lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env_keys[key.strip()] = value.strip()

    # 更新变更
    field_to_env = {
        "min_param_b": "MIN_PARAM_B",
        "model_refresh_interval": "MODEL_REFRESH_INTERVAL",
        "log_level": "LOG_LEVEL",
        "virtual_model_name": "VIRTUAL_MODEL_NAME",
        "modelscope_api_key": "MODELSCOPE_API_KEY",
        "admin_password": "ADMIN_PASSWORD",
        "log_retention_days": "LOG_RETENTION_DAYS",
        "show_model_tag": "SHOW_MODEL_TAG",
    }

    for field, change in changes.items():
        env_key = field_to_env.get(field)
        if env_key:
            env_keys[env_key] = str(change["new"])

    # 写回
    lines = [f"{k}={v}" for k, v in env_keys.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── 启动时间记录 ──────────────────────────────────────
def record_start_time():
    """记录服务启动时间（在 main.py 启动时调用）"""
    start_file = settings.data_dir / "start_time.json"
    start_file.write_text(
        json.dumps({"start_time": datetime.now().isoformat()}, ensure_ascii=False),
        encoding="utf-8",
    )


# ── Token 使用量统计 ───────────────────────────────────
@router.get("/api/stats")
async def get_stats(username: str = Depends(require_auth)):
    """获取 API 调用和 Token 使用量统计（需要认证）"""
    return JSONResponse(content=stats_collector.get_summary())


@router.post("/api/stats/reset")
async def reset_stats(username: str = Depends(require_auth)):
    """重置统计数据（需要认证）"""
    stats_collector.reset()
    logger.info(f"管理员 {username} 重置了统计数据")
    return JSONResponse(content={"message": "统计数据已重置"})
