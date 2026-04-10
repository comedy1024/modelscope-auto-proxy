"""
模型管理模块 — 管理可用模型列表、故障标记、自动刷新。
- 每天定时刷新模型列表
- 当某个模型返回 400/500 错误时，标记为今日不可用
- 自动切换到下一个可用模型
- 所有模型都不可用时返回错误
"""
import json
import logging
import threading
from datetime import datetime, date
from pathlib import Path
from config import settings
from model_fetcher import get_filtered_models

logger = logging.getLogger("model_manager")


class ModelManager:
    """管理可用模型列表和故障标记"""

    def __init__(self):
        self._lock = threading.Lock()
        self._models: list[dict] = []           # 当前可用模型列表（按参数量排序）
        self._disabled: dict[str, date] = {}    # model_id -> 禁用日期
        self._current_index: int = 0            # 当前使用的模型索引
        self._cache_file: Path = settings.data_dir / "model_cache.json"

    @property
    def models(self) -> list[dict]:
        with self._lock:
            return list(self._models)

    def refresh_models(self):
        """刷新模型列表（每天定时调用）"""
        logger.info("开始刷新模型列表...")

        new_models = get_filtered_models()
        if not new_models:
            logger.warning("刷新模型列表为空，保留旧列表")
            return

        with self._lock:
            old_ids = {m["id"] for m in self._models}
            new_ids = {m["id"] for m in new_models}

            added = new_ids - old_ids
            removed = old_ids - new_ids

            self._models = new_models
            self._current_index = 0

            # 清理过期禁用记录（非今天的）
            today = date.today()
            expired = [k for k, v in self._disabled.items() if v < today]
            for k in expired:
                del self._disabled[k]

            # 保存缓存
            self._save_cache()

        if added:
            logger.info(f"新增模型: {added}")
        if removed:
            logger.info(f"移除模型: {removed}")

        logger.info(f"模型列表已刷新，共 {len(new_models)} 个模型")

    def get_current_model(self) -> dict | None:
        """获取当前可用的模型（跳过被禁用的）"""
        with self._lock:
            today = date.today()

            # 清理过期的禁用标记
            expired = [k for k, v in self._disabled.items() if v < today]
            for k in expired:
                del self._disabled[k]
                logger.info(f"模型 {k} 禁用标记已过期，重新启用")

            # 从当前索引开始查找可用模型
            for i in range(len(self._models)):
                idx = (self._current_index + i) % len(self._models)
                model = self._models[idx]
                if model["id"] not in self._disabled:
                    self._current_index = idx
                    return model

            # 所有模型都被禁用
            logger.error(f"所有 {len(self._models)} 个模型今日均不可用！")
            return None

    def mark_disabled(self, model_id: str, reason: str = ""):
        """将模型标记为今日不可用"""
        with self._lock:
            today = date.today()
            self._disabled[model_id] = today

            # 切换到下一个可用模型
            for i in range(1, len(self._models)):
                next_idx = (self._current_index + i) % len(self._models)
                next_model = self._models[next_idx]
                if next_model["id"] not in self._disabled:
                    self._current_index = next_idx
                    break

            remaining = sum(
                1 for m in self._models if m["id"] not in self._disabled
            )

        logger.warning(
            f"模型 {model_id} 已标记为今日不可用 (原因: {reason})，"
            f"剩余可用模型: {remaining}/{len(self._models)}"
        )

    def get_status(self) -> dict:
        """获取当前模型管理状态"""
        with self._lock:
            today = date.today()
            active = [m for m in self._models if m["id"] not in self._disabled]
            disabled = [
                {"id": mid, "disabled_date": d.isoformat()}
                for mid, d in self._disabled.items()
                if d >= today
            ]

            current = None
            if active:
                current = self._models[self._current_index]
                if current["id"] in self._disabled:
                    current = None

            return {
                "total": len(self._models),
                "active": len(active),
                "disabled_today": len(disabled),
                "current_model": current,
                "disabled_list": disabled,
                "models": [
                    {
                        **m,
                        "is_active": m["id"] not in self._disabled,
                    }
                    for m in self._models
                ],
            }

    def _save_cache(self):
        """将模型列表保存到本地缓存"""
        try:
            cache_data = {
                "updated_at": datetime.now().isoformat(),
                "models": self._models,
            }
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(
                json.dumps(cache_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.debug(f"模型缓存已保存到 {self._cache_file}")
        except Exception as e:
            logger.error(f"保存模型缓存失败: {e}")

    def load_cache(self) -> bool:
        """从本地缓存加载模型列表（启动时使用）"""
        if not self._cache_file.exists():
            return False

        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            models = data.get("models", [])
            if models:
                with self._lock:
                    self._models = models
                    self._current_index = 0
                logger.info(f"从缓存加载了 {len(models)} 个模型 (更新于 {data.get('updated_at', 'unknown')})")
                return True
        except Exception as e:
            logger.error(f"加载模型缓存失败: {e}")

        return False


# 全局单例
model_manager = ModelManager()
