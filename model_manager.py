"""
模型管理模块 — 管理可用模型列表、故障标记、自动刷新。
- 每天定时刷新模型列表
- 当某个模型返回 404/500 错误时，标记为今日不可用
- 当某个模型返回 400 时，给予短期冷却（可能是临时兼容问题）
- 当某个模型遇到 429 限速时，立即给予短期冷却（避免重复选中）
- 连续 429 超过阈值时，视为每日额度耗尽，标记为今日不可用
- 自动切换到下一个可用模型
- 所有模型都不可用时返回错误

魔搭 API 限流规则参考:
- 每账号 2000 次/天，每模型 500 次/天（可能动态调整至 100 次）
- 存在未公开的短时 QPM/并发限制，大模型更严格
- 429 可能是短时频率超限（短暂冷却即可恢复）或每日额度用完（需等到 0 点重置）
"""
import json
import logging
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
from config import settings
from model_fetcher import get_filtered_models

logger = logging.getLogger("model_manager")

# 连续 429 超过此次数，视为每日额度耗尽，标记为今日不可用
_429_THRESHOLD = 3
# 首次 429 冷却时长（秒）— 2 分钟，避免短期内重复选中
_429_COOLDOWN_SECS = 120
# 400 错误冷却时长（秒）— 5 分钟，可能是临时兼容问题
_400_COOLDOWN_SECS = 300


class ModelManager:
    """管理可用模型列表和故障标记"""

    def __init__(self):
        self._lock = threading.Lock()
        self._models: list[dict] = []           # 当前可用模型列表（按参数量排序）
        self._disabled: dict[str, date] = {}    # model_id -> 禁用日期（今日不可用）
        self._cooldown: dict[str, datetime] = {} # model_id -> 冷却解除时间（429 限速）
        self._429_count: dict[str, int] = {}    # model_id -> 连续 429 次数
        self._current_index: int = 0            # 当前使用的模型索引
        self._cache_file: Path = settings.data_dir / "model_cache.json"
        self._custom_file: Path = settings.data_dir / "custom_models.json"
        # 自定义添加的模型（不在自动筛选结果中，用户手动添加）
        self._custom_include: list[dict] = []   # [{"id": "xxx/yyy", "param_b": 72.0}, ...]
        # 自定义屏蔽的模型（在自动筛选结果中，用户手动屏蔽）
        self._custom_exclude: set[str] = set()  # {"xxx/yyy", ...}
        # 加载自定义配置
        self._load_custom()

    @property
    def models(self) -> list[dict]:
        with self._lock:
            return list(self._models)

    def _is_available(self, model_id: str) -> bool:
        """检查模型是否当前可用（含今日禁用 + 冷却状态）"""
        if model_id in self._disabled:
            return False
        if model_id in self._cooldown:
            if datetime.now() < self._cooldown[model_id]:
                return False
            else:
                # 冷却已过期，清除
                del self._cooldown[model_id]
                if model_id in self._429_count:
                    del self._429_count[model_id]
                logger.info(f"模型 {model_id} 429 冷却已结束，重新可用")
        return True

    def refresh_models(self):
        """刷新模型列表（每天定时调用），合并自定义添加/屏蔽"""
        logger.info("开始刷新模型列表...")

        new_models = get_filtered_models()
        if not new_models:
            logger.warning("刷新模型列表为空，保留旧列表")
            return

        with self._lock:
            # 应用自定义屏蔽：移除被用户屏蔽的模型
            if self._custom_exclude:
                before = len(new_models)
                new_models = [m for m in new_models if m["id"] not in self._custom_exclude]
                removed = before - len(new_models)
                if removed > 0:
                    logger.info(f"自定义屏蔽移除了 {removed} 个模型")

            # 应用自定义添加：追加用户手动添加的模型（避免重复）
            existing_ids = {m["id"] for m in new_models}
            for cm in self._custom_include:
                if cm["id"] not in existing_ids:
                    new_models.append(cm)
                    existing_ids.add(cm["id"])
                    logger.info(f"自定义添加模型: {cm['id']} ({cm.get('param_b', '?')}B)")

            # 重新按参数量排序
            new_models.sort(key=lambda x: x["param_b"], reverse=True)

            old_ids = {m["id"] for m in self._models}
            new_ids = {m["id"] for m in new_models}

            added = new_ids - old_ids
            removed_ids = old_ids - new_ids

            self._models = new_models
            self._current_index = 0

            # 清理过期禁用记录（非今天的）
            today = date.today()
            expired = [k for k, v in self._disabled.items() if v < today]
            for k in expired:
                del self._disabled[k]

            # 清理已过期的冷却记录
            now = datetime.now()
            expired_cd = [k for k, v in self._cooldown.items() if v <= now]
            for k in expired_cd:
                del self._cooldown[k]
                self._429_count.pop(k, None)

            # 保存缓存
            self._save_cache()

        if added:
            logger.info(f"新增模型: {added}")
        if removed_ids:
            logger.info(f"移除模型: {removed_ids}")

        logger.info(f"模型列表已刷新，共 {len(new_models)} 个模型")

    def get_current_model(self) -> dict | None:
        """获取当前可用的模型（跳过被禁用/冷却中的）"""
        with self._lock:
            today = date.today()

            # 清理过期的今日禁用标记
            expired = [k for k, v in self._disabled.items() if v < today]
            for k in expired:
                del self._disabled[k]
                logger.info(f"模型 {k} 禁用标记已过期，重新启用")

            # 从当前索引开始查找可用模型
            for i in range(len(self._models)):
                idx = (self._current_index + i) % len(self._models)
                model = self._models[idx]
                if self._is_available(model["id"]):
                    self._current_index = idx
                    return model

            # 所有模型都被禁用
            logger.error(f"所有 {len(self._models)} 个模型当前均不可用！")
            return None

    def mark_disabled(self, model_id: str, reason: str = ""):
        """将模型标记为今日不可用（404/500 类永久性故障）"""
        with self._lock:
            today = date.today()
            self._disabled[model_id] = today

            # 同时清除429计数和冷却
            self._429_count.pop(model_id, None)
            self._cooldown.pop(model_id, None)

            # 切换到下一个可用模型
            for i in range(1, len(self._models)):
                next_idx = (self._current_index + i) % len(self._models)
                next_model = self._models[next_idx]
                if self._is_available(next_model["id"]):
                    self._current_index = next_idx
                    break

            remaining = sum(1 for m in self._models if self._is_available(m["id"]))

        logger.warning(
            f"模型 {model_id} 已标记为今日不可用 (原因: {reason})，"
            f"剩余可用模型: {remaining}/{len(self._models)}"
        )

    def mark_cooldown(self, model_id: str, reason: str = ""):
        """将模型标记为短期冷却（400 等可能是临时兼容问题，不应永久禁用）"""
        with self._lock:
            cooldown_until = datetime.now() + timedelta(seconds=_400_COOLDOWN_SECS)
            self._cooldown[model_id] = cooldown_until

            # 切换到下一个可用模型
            for i in range(1, len(self._models)):
                next_idx = (self._current_index + i) % len(self._models)
                next_model = self._models[next_idx]
                if self._is_available(next_model["id"]):
                    self._current_index = next_idx
                    break

            remaining = sum(1 for m in self._models if self._is_available(m["id"]))

        logger.warning(
            f"模型 {model_id} 给予 {_400_COOLDOWN_SECS // 60} 分钟冷却 (原因: {reason})，"
            f"剩余可用模型: {remaining}/{len(self._models)}"
        )

    def mark_429(self, model_id: str) -> bool:
        """
        记录 429 限速，立即给予短期冷却并切换模型。
        首次 429: 2 分钟冷却（避免短期内重复选中同一个被限速的模型）
        连续 N 次 429: 视为每日额度耗尽，标记为今日不可用（需等到 0 点重置）
        返回 True 表示已触发今日禁用，False 表示仅短期冷却。
        """
        with self._lock:
            count = self._429_count.get(model_id, 0) + 1
            self._429_count[model_id] = count

            if count >= _429_THRESHOLD:
                # 连续多次 429，视为每日额度耗尽，标记为今日不可用
                today = date.today()
                self._disabled[model_id] = today
                self._429_count.pop(model_id, None)
                self._cooldown.pop(model_id, None)

                remaining = sum(1 for m in self._models if self._is_available(m["id"]))
                is_disabled = True

                logger.warning(
                    f"模型 {model_id} 连续 {count} 次 429，视为每日额度耗尽，标记为今日不可用，"
                    f"剩余可用模型: {remaining}/{len(self._models)}"
                )
            else:
                # 首次/前几次 429，短期冷却
                cooldown_until = datetime.now() + timedelta(seconds=_429_COOLDOWN_SECS)
                self._cooldown[model_id] = cooldown_until
                is_disabled = False

                remaining = sum(1 for m in self._models if self._is_available(m["id"]))

                logger.warning(
                    f"模型 {model_id} 遭遇 429 (第 {count}/{_429_THRESHOLD} 次)，"
                    f"冷却 {_429_COOLDOWN_SECS // 60} 分钟，切换到下一个模型，"
                    f"剩余可用: {remaining}/{len(self._models)}"
                )

            # 切换到下一个可用模型
            for i in range(1, len(self._models)):
                next_idx = (self._current_index + i) % len(self._models)
                next_model = self._models[next_idx]
                if self._is_available(next_model["id"]):
                    self._current_index = next_idx
                    break

            return is_disabled

    def reset_429(self, model_id: str):
        """模型成功响应后，重置其 429 计数"""
        with self._lock:
            self._429_count.pop(model_id, None)

    def get_status(self) -> dict:
        """获取当前模型管理状态"""
        with self._lock:
            today = date.today()
            now = datetime.now()

            active = [m for m in self._models if self._is_available(m["id"])]
            disabled = [
                {"id": mid, "disabled_date": d.isoformat()}
                for mid, d in self._disabled.items()
                if d >= today
            ]
            cooldown_list = [
                {
                    "id": mid,
                    "cooldown_until": until.isoformat(),
                    "remaining_secs": max(0, int((until - now).total_seconds())),
                }
                for mid, until in self._cooldown.items()
                if until > now
            ]

            current = None
            if self._models:
                current = self._models[self._current_index]
                if not self._is_available(current["id"]):
                    current = None

            return {
                "total": len(self._models),
                "active": len(active),
                "disabled_today": len(disabled),
                "cooldown_count": len(cooldown_list),
                "current_model": current,
                "disabled_list": disabled,
                "cooldown_list": cooldown_list,
                "custom_include": list(self._custom_include),
                "custom_exclude": list(self._custom_exclude),
                "models": [
                    {
                        **m,
                        "is_active": self._is_available(m["id"]),
                        "is_cooldown": m["id"] in self._cooldown and self._cooldown[m["id"]] > now,
                        "is_disabled": m["id"] in self._disabled,
                        "is_custom": m["id"] in {c["id"] for c in self._custom_include},
                        "is_blocked": m["id"] in self._custom_exclude,
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

    # ── 自定义添加/屏蔽 ──────────────────────────────────────

    def add_custom_model(self, model_id: str, param_b: float = 0) -> dict:
        """
        手动添加一个模型到可用列表。
        如果模型已在列表中，返回提示；否则添加到 _custom_include 并持久化。
        """
        with self._lock:
            # 检查是否已存在
            existing_ids = {m["id"] for m in self._models}
            if model_id in existing_ids:
                return {"message": f"模型 {model_id} 已在列表中", "already_exists": True}

            # 检查是否在自定义屏蔽中
            if model_id in self._custom_exclude:
                return {"message": f"模型 {model_id} 在屏蔽列表中，请先解除屏蔽", "blocked": True}

            # 如果未提供参数量，尝试解析
            if param_b <= 0:
                from model_fetcher import parse_param_size, fetch_model_detail, estimate_param_from_storage
                param_b = parse_param_size(model_id)
                if param_b == 0:
                    detail = fetch_model_detail(model_id)
                    if detail:
                        ss = detail.get("StorageSize", 0)
                        if ss:
                            param_b = estimate_param_from_storage(ss)
                if param_b == 0:
                    param_b = 100.0  # 兜底默认值

            model = {"id": model_id, "param_b": param_b}
            self._custom_include.append(model)
            self._models.append(model)

            # 重新排序
            self._models.sort(key=lambda x: x["param_b"], reverse=True)
            self._save_cache()
            self._save_custom()

        logger.info(f"管理员手动添加模型: {model_id} ({param_b}B)")
        return {"message": f"模型 {model_id} ({param_b}B) 已添加", "already_exists": False}

    def block_model(self, model_id: str) -> dict:
        """
        手动屏蔽一个模型（从可用列表中永久移除，直到解除屏蔽）。
        """
        with self._lock:
            # 检查是否已屏蔽
            if model_id in self._custom_exclude:
                return {"message": f"模型 {model_id} 已在屏蔽列表中", "already_blocked": True}

            self._custom_exclude.add(model_id)

            # 从可用列表中移除
            self._models = [m for m in self._models if m["id"] != model_id]
            # 如果是自定义添加的，也移除
            self._custom_include = [m for m in self._custom_include if m["id"] != model_id]
            # 清理禁用/冷却状态
            self._disabled.pop(model_id, None)
            self._cooldown.pop(model_id, None)
            self._429_count.pop(model_id, None)

            self._current_index = 0
            self._save_cache()
            self._save_custom()

        logger.info(f"管理员手动屏蔽模型: {model_id}")
        return {"message": f"模型 {model_id} 已屏蔽", "already_blocked": False}

    def unblock_model(self, model_id: str) -> dict:
        """
        解除屏蔽一个模型，需要刷新模型列表才能重新加入。
        """
        with self._lock:
            if model_id not in self._custom_exclude:
                return {"message": f"模型 {model_id} 不在屏蔽列表中", "not_blocked": True}

            self._custom_exclude.discard(model_id)
            self._save_custom()

        # 需要刷新才能重新拉取被屏蔽的模型
        logger.info(f"管理员解除屏蔽模型: {model_id}，需要刷新模型列表才能生效")
        return {"message": f"模型 {model_id} 已解除屏蔽，请刷新模型列表生效", "not_blocked": False}

    def remove_custom_model(self, model_id: str) -> dict:
        """
        移除手动添加的模型。
        """
        with self._lock:
            # 检查是否在自定义添加列表中
            custom_ids = [m["id"] for m in self._custom_include]
            if model_id not in custom_ids:
                return {"message": f"模型 {model_id} 不是手动添加的", "not_custom": True}

            self._custom_include = [m for m in self._custom_include if m["id"] != model_id]
            self._models = [m for m in self._models if m["id"] != model_id]

            self._current_index = 0
            self._save_cache()
            self._save_custom()

        logger.info(f"管理员移除手动添加的模型: {model_id}")
        return {"message": f"模型 {model_id} 已移除", "not_custom": False}

    def get_custom_models(self) -> dict:
        """获取自定义添加和屏蔽的模型列表"""
        with self._lock:
            return {
                "custom_include": list(self._custom_include),
                "custom_exclude": list(self._custom_exclude),
            }

    def _load_custom(self):
        """从本地文件加载自定义添加/屏蔽的模型"""
        if not self._custom_file.exists():
            return

        try:
            data = json.loads(self._custom_file.read_text(encoding="utf-8"))
            with self._lock:
                self._custom_include = data.get("custom_include", [])
                self._custom_exclude = set(data.get("custom_exclude", []))
            logger.info(f"加载自定义模型配置: {len(self._custom_include)} 个添加, {len(self._custom_exclude)} 个屏蔽")
        except Exception as e:
            logger.error(f"加载自定义模型配置失败: {e}")

    def _save_custom(self):
        """将自定义添加/屏蔽的模型保存到本地文件"""
        try:
            data = {
                "custom_include": self._custom_include,
                "custom_exclude": list(self._custom_exclude),
            }
            self._custom_file.parent.mkdir(parents=True, exist_ok=True)
            self._custom_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"保存自定义模型配置失败: {e}")


# 全局单例
model_manager = ModelManager()
