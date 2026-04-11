"""
ModelScope API 转换器 — 主入口

功能:
1. 每天定时获取 ModelScope 支持 api-inference 的大模型列表（>=4B）
2. 按参数量从大到小排序
3. 对外暴露单一虚拟模型名，内部自动切换转发
4. 遇到 400/500 错误自动标记并切换下一个模型
5. 所有模型不可用时返回 JSON 错误
6. 长期运行，输出日志
"""
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
from apscheduler.schedulers.background import BackgroundScheduler

from config import settings
from model_manager import model_manager
from api_proxy import proxy_chat_completions, proxy_models
from admin import router as admin_router, record_start_time


# ── 日志配置 ─────────────────────────────────────────────
def setup_logging():
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # 根 logger — 先清除已有 handler，防止重复（uvicorn 等会自动添加 handler）
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    # 控制台
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(log_format, date_format))

    # 文件日志（按天滚动）
    from logging.handlers import TimedRotatingFileHandler
    # log_retention_days=0 表示永不清空，设置较大的 backupCount；否则按配置的天数
    backup_count = 3650 if settings.log_retention_days == 0 else settings.log_retention_days
    file_handler = TimedRotatingFileHandler(
        filename=settings.log_dir / "modelscope-proxy.log",
        when="midnight",
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(log_format, date_format))

    root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # 降低第三方库日志级别
    for name in ["uvicorn", "uvicorn.access", "httpx", "httpcore", "apscheduler"]:
        logging.getLogger(name).setLevel(logging.WARNING)


setup_logging()
logger = logging.getLogger("main")


# ── 定时任务 ─────────────────────────────────────────────
scheduler = BackgroundScheduler()


def scheduled_refresh():
    """定时刷新模型列表"""
    logger.info("=== 定时刷新模型列表 ===")
    model_manager.refresh_models()


# ── 应用生命周期 ──────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动和关闭时的操作"""
    logger.info("=" * 60)
    logger.info("ModelScope API 转换器启动中...")
    logger.info(f"监听端口: {settings.proxy_port}")
    logger.info(f"虚拟模型名: {settings.virtual_model_name}")
    logger.info(f"模型参数下限: {settings.min_param_b}B")
    logger.info(f"刷新间隔: {settings.model_refresh_interval}s")
    logger.info(f"管理后台: http://localhost:{settings.proxy_port}/admin")
    logger.info(f"管理后台账号: {settings.admin_username}")
    logger.info(f"管理后台密码: {settings.admin_password}")
    logger.info("=" * 60)

    # 记录启动时间
    record_start_time()

    # 启动时先尝试从缓存加载
    if not model_manager.load_cache():
        logger.info("无缓存，首次获取模型列表...")
        model_manager.refresh_models()
    else:
        # 有缓存但也刷新一次
        model_manager.refresh_models()

    # 启动定时刷新
    scheduler.add_job(
        scheduled_refresh,
        "interval",
        seconds=settings.model_refresh_interval,
        id="model_refresh",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"定时刷新已启动 (间隔 {settings.model_refresh_interval}s)")

    yield

    # 关闭
    scheduler.shutdown(wait=False)
    logger.info("ModelScope API 转换器已关闭")


# ── FastAPI 应用 ──────────────────────────────────────────
app = FastAPI(
    title="ModelScope API 转换器",
    description="自动切换 ModelScope 可用模型的 API 代理",
    version="1.1.0",
    lifespan=lifespan,
)

# 注册 admin 管理后台路由
app.include_router(admin_router)


# ── 路由 ──────────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI 兼容的 chat completions 端点"""
    return await proxy_chat_completions(request)


@app.get("/v1/models")
async def list_models(request: Request):
    """列出可用模型"""
    return await proxy_models(request)


@app.get("/v1/status")
async def get_status():
    """获取模型管理状态"""
    status = model_manager.get_status()
    return JSONResponse(content=status)


@app.post("/v1/refresh")
async def force_refresh():
    """手动触发模型列表刷新"""
    model_manager.refresh_models()
    return JSONResponse(content={
        "message": "模型列表已刷新",
        "timestamp": datetime.now().isoformat(),
    })


# ── 首页 HTML 缓存 ─────────────────────────────────────────
_INDEX_HTML: Optional[str] = None


def _load_index_html() -> str:
    """加载并缓存 index.html"""
    global _INDEX_HTML
    if _INDEX_HTML is None:
        html_path = Path(__file__).parent / "index.html"
        _INDEX_HTML = html_path.read_text(encoding="utf-8")
    return _INDEX_HTML


@app.get("/", response_class=HTMLResponse)
async def root():
    """根路径，返回首页宣传页"""
    return HTMLResponse(content=_load_index_html())


@app.get("/api/info")
async def api_info():
    """API 信息端点（供程序化访问）"""
    return JSONResponse(content={
        "service": "ModelScope API 转换器",
        "version": "1.1.0",
        "virtual_model": settings.virtual_model_name,
        "status": "/v1/status",
        "models": "/v1/models",
        "chat": "/v1/chat/completions",
    })


# ── 启动 ──────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.proxy_port,
        log_level=settings.log_level.lower(),
        access_log=True,
    )
