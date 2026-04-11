"""
统计模块 — 记录 API 调用次数和 Token 使用量。
- 按模型统计请求数、成功/失败数、prompt/completion/total tokens
- 内存存储（重启清零），可通过管理后台查看
- 支持按小时/按天聚合的 token 趋势
"""
import threading
import logging
from datetime import datetime, date
from collections import defaultdict

logger = logging.getLogger("stats")


class StatsCollector:
    """API 调用统计收集器"""

    def __init__(self):
        self._lock = threading.Lock()

        # 按模型统计（累计）: model_id -> {requests, success, errors, prompt_tokens, completion_tokens}
        self._model_stats: dict[str, dict] = defaultdict(lambda: {
            "requests": 0,
            "success": 0,
            "errors_4xx": 0,
            "errors_5xx": 0,
            "errors_429": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        })

        # 整体汇总
        self._total_requests = 0
        self._total_success = 0
        self._total_errors = 0
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_tokens = 0

        # 按小时趋势: "YYYY-MM-DD HH" -> {requests, tokens}
        self._hourly: dict[str, dict] = defaultdict(lambda: {"requests": 0, "tokens": 0})

        # 统计开始时间
        self._start_time = datetime.now()

    def record_request(self, model_id: str):
        """记录一次请求发出"""
        with self._lock:
            self._model_stats[model_id]["requests"] += 1
            self._total_requests += 1

    def record_success(self, model_id: str, prompt_tokens: int = 0,
                       completion_tokens: int = 0, total_tokens: int = 0):
        """记录一次成功响应（含 token 用量）"""
        # 如果 total_tokens 没有，则用 prompt+completion
        if total_tokens == 0 and (prompt_tokens > 0 or completion_tokens > 0):
            total_tokens = prompt_tokens + completion_tokens

        with self._lock:
            s = self._model_stats[model_id]
            s["success"] += 1
            s["prompt_tokens"] += prompt_tokens
            s["completion_tokens"] += completion_tokens
            s["total_tokens"] += total_tokens

            self._total_success += 1
            self._total_prompt_tokens += prompt_tokens
            self._total_completion_tokens += completion_tokens
            self._total_tokens += total_tokens

            # 小时趋势
            hour_key = datetime.now().strftime("%Y-%m-%d %H")
            self._hourly[hour_key]["requests"] += 1
            self._hourly[hour_key]["tokens"] += total_tokens

    def record_error(self, model_id: str, status_code: int):
        """记录一次错误响应"""
        with self._lock:
            s = self._model_stats[model_id]
            if status_code == 429:
                s["errors_429"] += 1
            elif 400 <= status_code < 500:
                s["errors_4xx"] += 1
            else:
                s["errors_5xx"] += 1
            self._total_errors += 1

    def get_summary(self) -> dict:
        """获取统计汇总"""
        with self._lock:
            uptime_secs = (datetime.now() - self._start_time).total_seconds()

            # 按 total_tokens 倒序排列模型统计
            model_list = []
            for mid, s in self._model_stats.items():
                model_list.append({
                    "model_id": mid,
                    **s,
                })
            model_list.sort(key=lambda x: x["total_tokens"], reverse=True)

            # 最近 24 小时趋势
            trend = []
            for hour_key in sorted(self._hourly.keys())[-24:]:
                trend.append({
                    "hour": hour_key,
                    **self._hourly[hour_key],
                })

            return {
                "start_time": self._start_time.isoformat(),
                "uptime_secs": int(uptime_secs),
                "total_requests": self._total_requests,
                "total_success": self._total_success,
                "total_errors": self._total_errors,
                "success_rate": round(self._total_success / self._total_requests * 100, 1)
                if self._total_requests > 0 else 0.0,
                "total_prompt_tokens": self._total_prompt_tokens,
                "total_completion_tokens": self._total_completion_tokens,
                "total_tokens": self._total_tokens,
                "by_model": model_list,
                "hourly_trend": trend,
            }

    def reset(self):
        """重置所有统计（管理员操作）"""
        with self._lock:
            self._model_stats.clear()
            self._total_requests = 0
            self._total_success = 0
            self._total_errors = 0
            self._total_prompt_tokens = 0
            self._total_completion_tokens = 0
            self._total_tokens = 0
            self._hourly.clear()
            self._start_time = datetime.now()
        logger.info("统计数据已重置")


# 全局单例
stats_collector = StatsCollector()
