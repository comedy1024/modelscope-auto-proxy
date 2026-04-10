"""
模型获取模块 — 从 ModelScope API 获取支持 api-inference 的大语言模型列表，
按参数量从大到小排序，过滤掉不适合 vibe coding 的模型（图像/视频/多模态/推理专用/基座模型等）。
"""
import re
import logging
import httpx
from datetime import datetime
from config import settings

logger = logging.getLogger("model_fetcher")

# 需要排除的关键词（图像/视频/多模态/音频/Embedding/非编码友好模型）
EXCLUDE_KEYWORDS = [
    # 图像/视频/多模态/音频
    "vl", "vision", "image", "video", "audio", "speech",
    "whisper", "tts", "asr", "diffus", "paint", "draw",
    "music", "sdxl", "sd-", "stable-diffusion", "flux",
    "clip", "blip", "llava", "internvl", "cogvlm",
    "glm-4v", "qwen-vl", "yi-vl", "minicpm-v",
    "qvq",  # QVQ 视觉推理模型
    # Embedding/Rerank
    "embedding", "bge-", "rerank",
    # 专用领域模型（不适合通用编码）
    "compassjudger", "judger",  # 评判模型
    "xiyansql",  # SQL 专用
    "gui-owl",  # GUI 操作专用
    "ministral",  # 迷你版，能力偏弱
]

# 需要排除的完整模型 ID（不适合 vibe coding 的模型）
EXCLUDE_MODEL_IDS = {
    # 基座模型/预训练模型（无指令遵循能力）
    "PaddlePaddle/ERNIE-4.5-300B-A47B-PT",
    "PaddlePaddle/ERNIE-4.5-21B-A3B-PT",
    "Qwen/Qwen3-32B",
    "Qwen/Qwen3-14B",
    "Qwen/Qwen3-8B",
    "Qwen/Qwen3-4B",
    "Qwen/Qwen3.5-35B-A3B",
    "Qwen/Qwen3.5-27B",
    "Qwen/Qwen3.5-122B-A10B",
    "Qwen/Qwen3-30B-A3B",
    # Thinking 推理版（思维链极长，速度太慢不适合编码）
    "Qwen/Qwen3-235B-A22B-Thinking-2507",
    "Qwen/Qwen3-30B-A3B-Thinking-2507",
    "Qwen/Qwen3-Next-80B-A3B-Thinking",
    # 小参数 R1 蒸馏模型（编码能力不足）
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B",
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
    "deepseek-ai/DeepSeek-R1-Distill-Llama-8B",
    # 纯推理模型（思维链过长，编码体验差）
    "Qwen/QwQ-32B",
    "Qwen/QwQ-32B-Preview",
}

# 需要包含的关键词（确保是文本生成模型）
TEXT_MODEL_PATTERNS = [
    re.compile(r"instruct", re.IGNORECASE),
    re.compile(r"chat", re.IGNORECASE),
    re.compile(r"conversation", re.IGNORECASE),
]


def parse_param_size(model_id: str) -> float:
    """
    从模型 ID 中提取参数量（单位 B）。
    例如: Qwen/Qwen2.5-72B-Instruct -> 72.0
          deepseek-ai/DeepSeek-R1-0528 -> 671.0 (需要从元数据获取)
    """
    # 尝试匹配常见模式: 72B, 7b, 0.5B, 480B-A35B 等
    match = re.search(r"(\d+(?:\.\d+)?)\s*[Bb]", model_id)
    if match:
        return float(match.group(1))

    # 尝试匹配 "A" 模式 (MoE 激活参数): 480B-A35B -> 取总参数 480
    match = re.search(r"(\d+(?:\.\d+)?)B-A\d+B", model_id, re.IGNORECASE)
    if match:
        return float(match.group(1))

    # 无法解析时返回 0，后续会被过滤
    return 0.0


def is_text_model(model_id: str) -> bool:
    """判断模型是否适合 vibe coding（排除图像/视频/多模态/推理专用/基座模型等）"""
    model_lower = model_id.lower()

    # 关键词过滤
    for kw in EXCLUDE_KEYWORDS:
        if kw in model_lower:
            return False

    # 精确 ID 过滤（基座模型、预训练模型等）
    if model_id in EXCLUDE_MODEL_IDS:
        return False

    return True


def fetch_models_from_api() -> list[dict]:
    """
    从 ModelScope API 获取支持 api-inference 的模型列表。
    GET https://api-inference.modelscope.cn/v1/models
    """
    url = f"{settings.modelscope_base_url}/models"
    headers = {
        "Authorization": f"Bearer {settings.modelscope_api_key}",
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        models = data.get("data", [])
        logger.info(f"从 ModelScope API 获取到 {len(models)} 个模型")
        return models

    except httpx.HTTPStatusError as e:
        logger.error(f"获取模型列表失败 (HTTP {e.response.status_code}): {e}")
        return []
    except Exception as e:
        logger.error(f"获取模型列表异常: {e}")
        return []


def fetch_model_detail(model_id: str) -> dict | None:
    """
    获取单个模型的详细信息，用于补充参数量等元数据。
    如果 API 不支持，则返回 None，回退到 ID 解析。
    """
    url = f"{settings.modelscope_base_url}/models/{model_id}"
    headers = {
        "Authorization": f"Bearer {settings.modelscope_api_key}",
    }

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return None


def get_filtered_models() -> list[dict]:
    """
    获取过滤后的文本大模型列表，按参数量从大到小排序。
    返回格式: [{"id": "Qwen/Qwen2.5-72B-Instruct", "param_b": 72.0}, ...]
    """
    raw_models = fetch_models_from_api()
    if not raw_models:
        logger.warning("未获取到任何模型，返回空列表")
        return []

    filtered = []
    for m in raw_models:
        model_id = m.get("id", "")
        if not model_id:
            continue

        # 过滤非文本模型
        if not is_text_model(model_id):
            logger.debug(f"排除非文本模型: {model_id}")
            continue

        # 解析参数量
        param_b = parse_param_size(model_id)

        # 尝试从详细 API 获取更精确的参数量
        if param_b == 0:
            detail = fetch_model_detail(model_id)
            if detail and "param_size" in detail:
                try:
                    param_b = float(detail["param_size"])
                except (ValueError, TypeError):
                    pass

        # 过滤小于阈值的模型
        if param_b < settings.min_param_b:
            logger.debug(f"排除小模型 (<{settings.min_param_b}B): {model_id} ({param_b}B)")
            continue

        filtered.append({
            "id": model_id,
            "param_b": param_b,
            "owned_by": m.get("owned_by", "unknown"),
            "created": m.get("created", 0),
        })

    # 按参数量从大到小排序
    filtered.sort(key=lambda x: x["param_b"], reverse=True)

    logger.info(f"过滤后保留 {len(filtered)} 个文本大模型 (>= {settings.min_param_b}B)")
    for i, m in enumerate(filtered[:5]):
        logger.info(f"  Top {i+1}: {m['id']} ({m['param_b']}B)")
    if len(filtered) > 5:
        logger.info(f"  ... 共 {len(filtered)} 个模型")

    return filtered


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    models = get_filtered_models()
    for m in models:
        print(f"{m['param_b']:>8.1f}B  {m['id']}")
