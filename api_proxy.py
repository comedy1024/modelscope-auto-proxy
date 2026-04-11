"""
API 转发模块 — 将 OpenAI 兼容格式的请求转发到 ModelScope API-Inference。
- 自动选择可用模型
- 遇到 404/500 错误自动标记模型为今日不可用并切换下一个模型重试
- 遇到 400 错误给予短期冷却（可能是临时兼容问题，非永久故障）
- 遇到 429 限速，切换下一个模型；连续 N 次后视为每日额度耗尽，标记今日不可用
- 支持流式和非流式响应
- 所有模型都不可用时返回 JSON 错误
- 最大重试次数限制，防止无限递归
- 统计请求数和 token 使用量
- 非 vibe-coding 纯文本模式下，在回复头部注入 [模型名] 标识
"""
import json
import logging
import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse, JSONResponse
from config import settings
from model_manager import model_manager
from stats import stats_collector

logger = logging.getLogger("api_proxy")

UPSTREAM_BASE = settings.modelscope_base_url

# 最大重试次数（最多切换多少个模型）
MAX_RETRIES = 10


def _short_model_name(model_id: str) -> str:
    """从 model_id 提取简短名称，例如 moonshotai/Kimi-K2.5 -> Kimi-K2.5"""
    return model_id.split("/")[-1]


def _inject_model_tag(content: str, model_id: str) -> str:
    """在文本回复开头注入模型标识"""
    tag = f"[{_short_model_name(model_id)}] "
    return tag + content


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
        logger.error("所有模型当前均不可用，返回错误")
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "所有模型当前均不可用，请稍后重试",
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

    # 统计：记录一次请求
    stats_collector.record_request(model["id"])

    # 判断是否流式
    is_stream = body.get("stream", False)

    # 构建上游请求
    headers = {
        "Authorization": f"Bearer {settings.modelscope_api_key}",
        "Content-Type": "application/json",
    }

    upstream_url = f"{UPSTREAM_BASE}/chat/completions"

    # 是否注入模型标识（运行时读取，支持热更新）
    show_tag = getattr(settings, "show_model_tag", False)

    try:
        if is_stream:
            return await _proxy_stream(upstream_url, headers, body, model["id"], request, _retry_count, show_tag)
        else:
            return await _proxy_non_stream(upstream_url, headers, body, model["id"], request, _retry_count, show_tag)
    except httpx.TimeoutException:
        logger.warning(f"模型 {model['id']} 请求超时，切换下一个")
        stats_collector.record_error(model["id"], 504)
        model_manager.mark_disabled(model["id"], "请求超时")
        return await proxy_chat_completions(request, _retry_count + 1)
    except httpx.ConnectError as e:
        logger.warning(f"模型 {model['id']} 连接失败: {e}，切换下一个")
        stats_collector.record_error(model["id"], 503)
        model_manager.mark_disabled(model["id"], f"连接失败: {e}")
        return await proxy_chat_completions(request, _retry_count + 1)
    except Exception as e:
        logger.error(f"模型 {model['id']} 请求异常: {e}，切换下一个")
        stats_collector.record_error(model["id"], 500)
        model_manager.mark_disabled(model["id"], f"请求异常: {e}")
        return await proxy_chat_completions(request, _retry_count + 1)


def _extract_usage(resp_data: dict) -> tuple[int, int, int]:
    """从响应 JSON 中提取 usage 字段，返回 (prompt_tokens, completion_tokens, total_tokens)"""
    usage = resp_data.get("usage", {})
    if not usage:
        return 0, 0, 0
    return (
        int(usage.get("prompt_tokens", 0)),
        int(usage.get("completion_tokens", 0)),
        int(usage.get("total_tokens", 0)),
    )


async def _proxy_non_stream(
    url: str, headers: dict, body: dict, model_id: str,
    request: Request, retry_count: int, show_tag: bool = False
) -> Response:
    """非流式转发"""
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, headers=headers, json=body)

    if resp.status_code in (404, 500, 502, 503):
        error_msg = f"HTTP {resp.status_code}"
        try:
            error_detail = resp.json()
            error_msg = f"HTTP {resp.status_code}: {json.dumps(error_detail, ensure_ascii=False)[:300]}"
        except Exception:
            error_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"

        logger.warning(f"模型 {model_id} 返回不可恢复错误: {error_msg}，标记为今日不可用")
        stats_collector.record_error(model_id, resp.status_code)
        model_manager.mark_disabled(model_id, error_msg)
        return await proxy_chat_completions(request, retry_count + 1)

    # 400 — 可能是临时兼容问题，给予短期冷却而非永久禁用
    if resp.status_code == 400:
        error_msg = f"HTTP 400"
        try:
            error_detail = resp.json()
            error_msg = f"HTTP 400: {json.dumps(error_detail, ensure_ascii=False)[:300]}"
        except Exception:
            error_msg = f"HTTP 400: {resp.text[:200]}"

        logger.warning(f"模型 {model_id} 返回 400 错误: {error_msg}，给予短期冷却")
        stats_collector.record_error(model_id, 400)
        model_manager.mark_cooldown(model_id, error_msg)
        return await proxy_chat_completions(request, retry_count + 1)

    # 429 — 限速，切换模型重试
    if resp.status_code == 429:
        logger.warning(f"模型 {model_id} 返回 429 限速，切换下一个")
        stats_collector.record_error(model_id, 429)
        model_manager.mark_429(model_id)
        return await proxy_chat_completions(request, retry_count + 1)

    if resp.status_code >= 400:
        # 其他错误（如 401/403）不标记禁用，直接返回
        logger.error(f"模型 {model_id} 返回不可重试错误: HTTP {resp.status_code}")
        stats_collector.record_error(model_id, resp.status_code)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={"Content-Type": "application/json"},
        )

    # 成功 — 提取 usage 统计
    try:
        resp_data = resp.json()
        prompt_t, comp_t, total_t = _extract_usage(resp_data)
        stats_collector.record_success(model_id, prompt_t, comp_t, total_t)
        model_manager.reset_429(model_id)

        # 注入模型标识（如果开启且为纯文本模式）
        if show_tag:
            resp_data = _inject_tag_to_response(resp_data, model_id)
            resp_content = json.dumps(resp_data, ensure_ascii=False).encode("utf-8")
        else:
            resp_content = resp.content

    except Exception:
        # 解析失败则直接透传（不影响正常响应）
        stats_collector.record_success(model_id)
        resp_content = resp.content

    logger.info(f"模型 {model_id} 请求成功")
    return Response(
        content=resp_content,
        status_code=resp.status_code,
        headers={"Content-Type": "application/json"},
    )


def _inject_tag_to_response(resp_data: dict, model_id: str) -> dict:
    """
    在非流式响应的 choices[0].message.content 头部注入 [模型名] 标识。
    仅在 content 为字符串时注入（跳过 function_call / tool_calls 等结构体）。
    """
    try:
        choices = resp_data.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            content = msg.get("content")
            if isinstance(content, str) and content:
                msg["content"] = _inject_model_tag(content, model_id)
                choices[0]["message"] = msg
                resp_data["choices"] = choices
    except Exception:
        pass
    return resp_data


async def _proxy_stream(
    url: str, headers: dict, body: dict, model_id: str,
    request: Request, retry_count: int, show_tag: bool = False
) -> StreamingResponse | JSONResponse:
    """流式转发"""
    # 创建长生命周期的客户端，不使用 async with，以便流式传输完成后才关闭
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0))

    try:
        req = client.stream("POST", url, headers=headers, json=body)
        resp = await req.__aenter__()

        if resp.status_code in (404, 500, 502, 503):
            error_body = await resp.aread()
            await req.__aexit__(None, None, None)
            await client.aclose()
            error_msg = f"HTTP {resp.status_code}: {error_body.decode('utf-8', errors='replace')[:200]}"
            logger.warning(f"模型 {model_id} 流式请求不可恢复错误: {error_msg}，标记为今日不可用")
            stats_collector.record_error(model_id, resp.status_code)
            model_manager.mark_disabled(model_id, error_msg)
            return await proxy_chat_completions(request, retry_count + 1)

        # 400 — 可能是临时兼容问题，给予短期冷却
        if resp.status_code == 400:
            error_body = await resp.aread()
            await req.__aexit__(None, None, None)
            await client.aclose()
            error_msg = f"HTTP 400: {error_body.decode('utf-8', errors='replace')[:200]}"
            logger.warning(f"模型 {model_id} 流式请求 400 错误: {error_msg}，给予短期冷却")
            stats_collector.record_error(model_id, 400)
            model_manager.mark_cooldown(model_id, error_msg)
            return await proxy_chat_completions(request, retry_count + 1)

        # 429 — 限速，切换模型重试
        if resp.status_code == 429:
            error_body = await resp.aread()
            await req.__aexit__(None, None, None)
            await client.aclose()
            logger.warning(f"模型 {model_id} 流式请求 429 限速，切换下一个")
            stats_collector.record_error(model_id, 429)
            model_manager.mark_429(model_id)
            return await proxy_chat_completions(request, retry_count + 1)

        if resp.status_code >= 400:
            error_body = await resp.aread()
            await req.__aexit__(None, None, None)
            await client.aclose()
            logger.error(f"模型 {model_id} 返回不可重试错误: HTTP {resp.status_code}")
            stats_collector.record_error(model_id, resp.status_code)
            return Response(content=error_body, status_code=resp.status_code)

        # 成功 — 流式转发
        # 流式模式：注入标识在第一个内容 chunk，同时统计 token（从 [DONE] 前的 usage chunk 提取）
        first_chunk_done = False
        usage_buffer = []  # 缓存最后几个 chunk 用于提取 usage

        async def stream_generator():
            nonlocal first_chunk_done
            try:
                async for chunk in resp.aiter_bytes():
                    if show_tag and not first_chunk_done:
                        # 尝试在第一个有 content 的 delta chunk 注入标识
                        injected = _try_inject_tag_stream_chunk(chunk, model_id)
                        if injected is not None:
                            first_chunk_done = True
                            yield injected
                            continue
                    # 缓存最后 3 个 chunk（用于提取 usage）
                    usage_buffer.append(chunk)
                    if len(usage_buffer) > 3:
                        usage_buffer.pop(0)
                    yield chunk
            finally:
                # 尝试从最后几个 chunk 提取 usage
                _extract_and_record_stream_usage(usage_buffer, model_id)
                await req.__aexit__(None, None, None)
                await client.aclose()

        model_manager.reset_429(model_id)
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


def _try_inject_tag_stream_chunk(chunk: bytes, model_id: str) -> bytes | None:
    """
    尝试在流式 SSE chunk 的第一个有内容的 delta 中注入模型标识。
    成功时返回修改后的 chunk bytes，失败/不适用时返回 None。
    """
    try:
        text = chunk.decode("utf-8")
        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                return None
            data = json.loads(data_str)
            choices = data.get("choices", [])
            if not choices:
                return None
            delta = choices[0].get("delta", {})
            content = delta.get("content")
            if isinstance(content, str) and content:
                delta["content"] = _inject_model_tag(content, model_id)
                choices[0]["delta"] = delta
                data["choices"] = choices
                new_data_str = json.dumps(data, ensure_ascii=False)
                new_line = f"data: {new_data_str}"
                new_text = text.replace(line, new_line, 1)
                return new_text.encode("utf-8")
    except Exception:
        pass
    return None


def _extract_and_record_stream_usage(chunks: list[bytes], model_id: str):
    """从流式最后几个 chunk 尝试提取 usage 并记录统计"""
    try:
        for chunk in reversed(chunks):
            text = chunk.decode("utf-8", errors="replace")
            for line in text.split("\n"):
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    continue
                try:
                    data = json.loads(data_str)
                    if "usage" in data:
                        p, c, t = _extract_usage(data)
                        stats_collector.record_success(model_id, p, c, t)
                        return
                except Exception:
                    continue
        # 没有找到 usage chunk，仍然记录一次成功（不含 token 数）
        stats_collector.record_success(model_id)
    except Exception:
        stats_collector.record_success(model_id)


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
