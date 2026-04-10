"""
API 转发模块 — 将 OpenAI 兼容格式的请求转发到 ModelScope API-Inference。
- 自动选择可用模型
- 遇到 400/500 错误自动标记模型并切换下一个模型重试
- 支持流式和非流式响应
- 所有模型都不可用时返回 JSON 错误
- 最大重试次数限制，防止无限递归
"""
import json
import logging
import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse, JSONResponse
from config import settings
from model_manager import model_manager

logger = logging.getLogger("api_proxy")

UPSTREAM_BASE = settings.modelscope_base_url

# 最大重试次数（最多切换多少个模型）
MAX_RETRIES = 10


async def proxy_chat_completions(request: Request, _retry_count: int = 0) -> Response:
    """
    转发 /v1/chat/completions 请求。
    自动替换 model 字段为当前可用的 ModelScope 模型。
    遇到错误自动标记并重试下一个模型。
    """
    # 超过最大重试次数
    if _retry_count >= MAX_RETRIES:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": f"已尝试 {MAX_RETRIES} 个模型均失败，请稍后重试",
                    "type": "service_unavailable",
                    "code": "max_retries_exceeded",
                }
            },
        )

    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": f"无效的请求体: {e}", "type": "invalid_request_error"}},
        )

    # 获取可用模型
    model = model_manager.get_current_model()
    if model is None:
        logger.error("所有模型今日均不可用，返回错误")
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "所有模型今日均不可用，请明天再试",
                    "type": "service_unavailable",
                    "code": "all_models_disabled",
                }
            },
        )

    # 替换模型名称
    original_model = body.get("model", "")
    body["model"] = model["id"]
    logger.info(
        f"[重试={_retry_count}] 转发请求: model={model['id']} ({model['param_b']}B), "
        f"原始model={original_model}"
    )

    # 判断是否流式
    is_stream = body.get("stream", False)

    # 构建上游请求
    headers = {
        "Authorization": f"Bearer {settings.modelscope_api_key}",
        "Content-Type": "application/json",
    }

    upstream_url = f"{UPSTREAM_BASE}/chat/completions"

    try:
        if is_stream:
            return await _proxy_stream(upstream_url, headers, body, model["id"], request, _retry_count)
        else:
            return await _proxy_non_stream(upstream_url, headers, body, model["id"], request, _retry_count)
    except httpx.TimeoutException:
        logger.warning(f"模型 {model['id']} 请求超时，切换下一个")
        model_manager.mark_disabled(model["id"], "请求超时")
        return await proxy_chat_completions(request, _retry_count + 1)
    except httpx.ConnectError as e:
        logger.warning(f"模型 {model['id']} 连接失败: {e}，切换下一个")
        model_manager.mark_disabled(model["id"], f"连接失败: {e}")
        return await proxy_chat_completions(request, _retry_count + 1)
    except Exception as e:
        logger.error(f"模型 {model['id']} 请求异常: {e}，切换下一个")
        model_manager.mark_disabled(model["id"], f"请求异常: {e}")
        return await proxy_chat_completions(request, _retry_count + 1)


async def _proxy_non_stream(
    url: str, headers: dict, body: dict, model_id: str,
    request: Request, retry_count: int
) -> Response:
    """非流式转发"""
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, headers=headers, json=body)

    if resp.status_code in (400, 500, 502, 503):
        error_msg = f"HTTP {resp.status_code}"
        try:
            error_detail = resp.json()
            error_msg = f"HTTP {resp.status_code}: {json.dumps(error_detail, ensure_ascii=False)[:300]}"
        except Exception:
            error_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"

        logger.warning(f"模型 {model_id} 返回错误: {error_msg}，切换下一个")
        model_manager.mark_disabled(model_id, error_msg)
        return await proxy_chat_completions(request, retry_count + 1)

    if resp.status_code >= 400:
        # 其他错误（如 401/403）不标记禁用，直接返回
        logger.error(f"模型 {model_id} 返回不可重试错误: HTTP {resp.status_code}")
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={"Content-Type": "application/json"},
        )

    logger.info(f"模型 {model_id} 请求成功")
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={"Content-Type": "application/json"},
    )


async def _proxy_stream(
    url: str, headers: dict, body: dict, model_id: str,
    request: Request, retry_count: int
) -> StreamingResponse | JSONResponse:
    """流式转发"""
    # 创建长生命周期的客户端，不使用 async with，以便流式传输完成后才关闭
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0))

    try:
        req = client.stream("POST", url, headers=headers, json=body)
        resp = await req.__aenter__()

        if resp.status_code in (400, 500, 502, 503):
            error_body = await resp.aread()
            await req.__aexit__(None, None, None)
            await client.aclose()
            error_msg = f"HTTP {resp.status_code}: {error_body.decode('utf-8', errors='replace')[:200]}"
            logger.warning(f"模型 {model_id} 流式请求错误: {error_msg}，切换下一个")
            model_manager.mark_disabled(model_id, error_msg)
            return await proxy_chat_completions(request, retry_count + 1)

        if resp.status_code >= 400:
            error_body = await resp.aread()
            await req.__aexit__(None, None, None)
            await client.aclose()
            logger.error(f"模型 {model_id} 返回不可重试错误: HTTP {resp.status_code}")
            return Response(content=error_body, status_code=resp.status_code)

        # 成功 — 流式转发
        async def stream_generator():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await req.__aexit__(None, None, None)
                await client.aclose()

        return StreamingResponse(
            stream_generator(),
            status_code=resp.status_code,
            headers={
                "Content-Type": resp.headers.get("content-type", "text/event-stream"),
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
    except Exception as e:
        await client.aclose()
        raise


async def proxy_models(request: Request) -> Response:
    """返回我们管理的模型列表（OpenAI 格式）"""
    status = model_manager.get_status()

    model_list = []
    for m in status["models"]:
        model_list.append({
            "id": m["id"],
            "object": "model",
            "owned_by": m.get("owned_by", "unknown"),
            "created": m.get("created", 0),
            "param_b": m.get("param_b", 0),
            "is_active": m.get("is_active", True),
        })

    return JSONResponse(content={
        "object": "list",
        "data": model_list,
    })
