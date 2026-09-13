"""
自适应速率限制 & 失败重试队列
"""
import time
import random
from dataclasses import dataclass, field
from typing import Callable


class AdaptiveRateLimiter:
    """自适应速率限制器

    策略:
    - 维护滑动窗口的成功/失败记录
    - 连续成功 N 次 -> 逐步减少延迟
    - 遇到 412/限流 -> 指数退避 + 提升基准延迟
    - 冷启动时使用保守延迟
    """

    def __init__(
        self,
        base_delay: float = 5.0,       # 基础延迟(秒)
        min_delay: float = 0.5,         # 最小延迟
        max_delay: float = 120.0,       # 最大延迟
        decrease_factor: float = 0.8,   # 成功时延迟衰减因子
        increase_factor: float = 2.0,   # 失败时延迟增长因子
        success_window: int = 5,         # 连续成功多少次后开始衰减
        jitter: float = 0.3,            # 抖动比例 (0.3 = ±30%)
    ):
        self.base_delay = base_delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.decrease_factor = decrease_factor
        self.increase_factor = increase_factor
        self.success_window = success_window
        self.jitter = jitter

        self._current_delay = base_delay
        self._consecutive_successes = 0
        self._consecutive_failures = 0
        self._total_successes = 0
        self._total_failures = 0

    @property
    def current_delay(self) -> float:
        return self._current_delay

    def _apply_jitter(self, delay: float) -> float:
        """添加随机抖动"""
        jitter_amount = delay * self.jitter
        return delay + random.uniform(-jitter_amount, jitter_amount)

    def record_success(self):
        """记录一次成功"""
        self._total_successes += 1
        self._consecutive_successes += 1
        self._consecutive_failures = 0

        # 连续成功达到窗口大小后，衰减延迟
        if self._consecutive_successes >= self.success_window:
            self._current_delay = max(
                self.min_delay,
                self._current_delay * self.decrease_factor,
            )
            self._consecutive_successes = 0  # 重置计数器

    def record_failure(self, is_rate_limit: bool = True):
        """记录一次失败

        Args:
            is_rate_limit: 是否是限流（412等），限流则指数退避
        """
        self._total_failures += 1
        self._consecutive_failures += 1
        self._consecutive_successes = 0

        if is_rate_limit:
            # 指数退避
            self._current_delay = min(
                self.max_delay,
                self._current_delay * self.increase_factor,
            )

    def wait(self):
        """执行一次自适应等待"""
        delay = self._apply_jitter(self._current_delay)
        delay = max(0, delay)  # 确保非负
        time.sleep(delay)

    def stats(self) -> dict:
        """获取统计信息"""
        return {
            "current_delay": round(self._current_delay, 2),
            "total_successes": self._total_successes,
            "total_failures": self._total_failures,
            "consecutive_successes": self._consecutive_successes,
            "consecutive_failures": self._consecutive_failures,
        }

    def reset(self):
        """重置到初始状态"""
        self._current_delay = self.base_delay
        self._consecutive_successes = 0
        self._consecutive_failures = 0


# ── 预设配置 ──────────────────────────────────────────────

def create_comment_limiter() -> AdaptiveRateLimiter:
    """评论接口限流器：页间延迟"""
    return AdaptiveRateLimiter(
        base_delay=5.0,
        min_delay=2.0,
        max_delay=15.0,
        decrease_factor=0.85,
        increase_factor=2.0,
        success_window=5,
    )


def create_danmaku_limiter() -> AdaptiveRateLimiter:
    """弹幕接口限流器：段间延迟"""
    return AdaptiveRateLimiter(
        base_delay=0.6,
        min_delay=0.3,
        max_delay=3.0,
        decrease_factor=0.8,
        increase_factor=2.0,
        success_window=5,
    )


def create_inter_video_limiter() -> AdaptiveRateLimiter:
    """视频间冷却限流器"""
    return AdaptiveRateLimiter(
        base_delay=30.0,
        min_delay=10.0,
        max_delay=90.0,
        decrease_factor=0.8,
        increase_factor=1.5,
        success_window=3,
    )


# ── 重试队列 ──────────────────────────────────────────────

@dataclass
class RetryItem:
    """待重试项"""
    aid: int
    title: str
    operation: str        # "comments" | "danmaku" | "full_video"
    error: str
    context: dict = field(default_factory=dict)
    retry_count: int = 0
    max_retries: int = 3


class RetryQueue:
    """失败重试队列"""

    def __init__(self):
        self._items: list[RetryItem] = []
        self._permanent_failures: list[RetryItem] = []

    def add(self, aid: int, title: str, operation: str, error: str,
            context: dict = None, max_retries: int = 3):
        """添加失败项到重试队列"""
        item = RetryItem(
            aid=aid,
            title=title,
            operation=operation,
            error=error,
            context=context or {},
            max_retries=max_retries,
        )
        self._items.append(item)

    def has_pending(self) -> bool:
        return len(self._items) > 0

    def get_pending(self) -> list[RetryItem]:
        """获取待重试项（不移除）"""
        return list(self._items)

    def get_pending_count(self) -> int:
        return len(self._items)

    def mark_retried(self, item: RetryItem, success: bool, new_error: str = ""):
        """标记重试结果"""
        if success:
            self._items.remove(item)
        else:
            item.retry_count += 1
            if new_error:
                item.error = new_error
            if item.retry_count >= item.max_retries:
                self._items.remove(item)
                self._permanent_failures.append(item)

    def generate_summary(self) -> dict:
        """生成错误汇总"""
        all_failures = self._items + self._permanent_failures
        return {
            "total_failures": len(all_failures),
            "retryable": len(self._items),
            "permanent": len(self._permanent_failures),
            "by_operation": self._group_by_operation(all_failures),
            "details": [
                {
                    "aid": item.aid,
                    "title": item.title,
                    "operation": item.operation,
                    "error": item.error,
                    "retried": item.retry_count,
                    "max_retries": item.max_retries,
                }
                for item in all_failures
            ],
        }

    def print_summary(self):
        """打印错误汇总"""
        summary = self.generate_summary()
        if summary["total_failures"] == 0:
            print("\n✅ 全部视频处理成功，无失败项")
            return

        print(f"\n{'─' * 50}")
        print(f"⚠️ 失败汇总: {summary['total_failures']} 项 "
              f"(可重试{summary['retryable']} / 已放弃{summary['permanent']})")
        print(f"{'─' * 50}")
        for item in summary["details"]:
            status = "🔄 可重试" if item["retried"] < item["max_retries"] else "❌ 已放弃"
            print(f"  {status} [{item['operation']}] "
                  f"{item['title'][:30]} (aid={item['aid']})")
            print(f"         错误: {item['error'][:80]}")

    @staticmethod
    def _group_by_operation(items: list[RetryItem]) -> dict:
        groups = {}
        for item in items:
            groups.setdefault(item.operation, 0)
            groups[item.operation] += 1
        return groups
