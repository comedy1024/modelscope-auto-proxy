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
    "deepseek-ai/DeepSeek-R1-0528",  # 推理模型，思维链极长，不适合 vibe coding
    # 已知不可用模型（持续 400/404 错误）
    "MiniMax/MiniMax-M1-80k",        # 持续返回 400 错误
    "LLM-Research/Llama-4-Maverick-17B-128E-Instruct",  # 持续返回 404，模型可能已下线
}

# ── 已知模型参数量映射表 ─────────────────────────────────────
# 对于 ID 中不含参数量标识的模型，手动维护其参数量。
# 格式: model_id -> (总参数B, 激活参数B)，如果只知道总参数则激活参数填 0。
# 优先级最高，覆盖所有自动解析。
KNOWN_MODEL_PARAMS = {
    # DeepSeek 系列
    "deepseek-ai/DeepSeek-V3.2": (685.0, 37.0),     # MoE 685B 总参数, ~37B 激活
    # MiniMax 系列
    "MiniMax/MiniMax-M2.5": (456.0, 45.0),           # MoE 456B 总参数
    # 智谱 GLM 系列
    "ZhipuAI/GLM-5": (744.0, 40.0),                  # MoE 744B 总参数, 40B 激活
    "ZhipuAI/GLM-4.7-Flash": (9.0, 9.0),             # GLM-4.7 Flash ~9B
    # 月之暗面
    "moonshotai/Kimi-K2.5": (1000.0, 60.0),          # MoE ~1T 总参数
    # 上海 AI 实验室
    "Shanghai_AI_Laboratory/Intern-S1": (107.0, 107.0),  # Intern-S1 ~107B
    "Shanghai_AI_Laboratory/Intern-S1-mini": (27.0, 27.0), # Intern-S1-mini ~27B
    # 阶跃星辰
    "stepfun-ai/Step-3.5-Flash": (80.0, 22.0),       # MoE ~80B
    # 小米
    "XiaomiMiMo/MiMo-V2-Flash": (17.0, 17.0),        # MiMo V2 Flash ~17B
    # 美团
    "meituan-longcat/LongCat-Flash-Lite": (27.0, 27.0), # ~27B
    # Mistral 系列（ID 中没有参数量标识）
    "mistralai/Mistral-Large-Instruct-2407": (123.0, 123.0),  # Mistral Large 123B
    "mistralai/Mistral-Small-Instruct-2409": (22.0, 22.0),    # Mistral Small 22B
    # Cohere
    "LLM-Research/c4ai-command-r-plus-08-2024": (104.0, 104.0),  # Command R+ 104B
    # Meta Llama 4 — Llama-4-Maverick 已下线(404)，仅保留映射以备重新上线
    # "LLM-Research/Llama-4-Maverick-17B-128E-Instruct": (400.0, 17.0),
}


def parse_param_size(model_id: str) -> float:
    """
    从模型 ID 中提取参数量（单位 B）。
    例如: Qwen/Qwen2.5-72B-Instruct -> 72.0
          deepseek-ai/DeepSeek-R1-0528 -> 0.0 (需要查映射表)
    """
    # 优先查映射表
    if model_id in KNOWN_MODEL_PARAMS:
        total, active = KNOWN_MODEL_PARAMS[model_id]
        return total

    # 尝试匹配常见模式: 72B, 7b, 0.5B, 480B-A35B 等
    match = re.search(r"(\d+(?:\.\d+)?)\s*[Bb]", model_id)
    if match:
        return float(match.group(1))

    # 尝试匹配 "A" 模式 (MoE 激活参数): 480B-A35B -> 取总参数 480
    match = re.search(r"(\d+(?:\.\d+)?)B-A\d+B", model_id, re.IGNORECASE)
    if match:
        return float(match.group(1))

    # 无法解析时返回 0，后续会尝试通过其他方式获取
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
    从 ModelScope hub API 获取模型详细信息（含 StorageSize）。
    用于估算无法从 ID 解析参数量的模型。
    """
    url = f"https://modelscope.cn/api/v1/models/{model_id}"

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json().get("Data", {})
                return data
    except Exception:
        pass
    return None


def estimate_param_from_storage(storage_size: int) -> float:
    """
    从模型存储大小估算参数量（单位 B）。
    粗略规则：
    - 纯 BF16: size / 2 = 参数数
    - FP8 混合: size / 1.2 ≈ 参数数
    - 纯 FP32: size / 4 = 参数数
    综合取 size / 1.5 作为折中估算，再转换为 B。
    """
    if storage_size <= 0:
        return 0.0
    # 折中估算: 假设平均每参数约 1.5 字节（混合精度）
    estimated_b = storage_size / 1.5 / 1e9
    return round(estimated_b, 1)


def get_filtered_models() -> list[dict]:
    """
    获取过滤后的文本大模型列表，按参数量从大到小排序。
    返回格式: [{"id": "Qwen/Qwen2.5-72B-Instruct", "param_b": 72.0}, ...]
    """
    raw_models = fetch_models_from_api()
    if not raw_models:
        logger.warning("未获取到任何模型，返回空列表")
        return []

    # 找出所有 param=0 的文本模型，需要从 hub API 获取详细信息
    models_need_detail = []
    for m in raw_models:
        model_id = m.get("id", "")
        if not model_id:
            continue
        if not is_text_model(model_id):
            continue
        # 映射表和 ID 解析都无法获取参数量
        if model_id not in KNOWN_MODEL_PARAMS and parse_param_size(model_id) == 0:
            models_need_detail.append(model_id)

    # 批量获取未知模型的详细信息（用于估算参数量）
    storage_map: dict[str, int] = {}
    if models_need_detail:
        logger.info(f"需要从 hub API 获取参数量的模型: {len(models_need_detail)} 个")
        for mid in models_need_detail:
            detail = fetch_model_detail(mid)
            if detail:
                ss = detail.get("StorageSize", 0)
                if ss:
                    storage_map[mid] = ss

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

        # 如果映射表和 ID 解析都失败，尝试从存储大小估算
        if param_b == 0:
            if model_id in storage_map:
                param_b = estimate_param_from_storage(storage_map[model_id])
                logger.info(f"从存储大小估算参数量: {model_id} -> {param_b}B (storage={storage_map[model_id] / 1e9:.1f}GB)")
            else:
                # 最后兜底: 既然通过了文本模型过滤且在 API 推理列表中，
                # 说明是可用大模型，给予一个保守默认值以确保不被过滤
                param_b = 100.0
                logger.info(f"无法获取参数量，使用默认值: {model_id} -> {param_b}B")

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
