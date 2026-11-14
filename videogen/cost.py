"""成本控制：提交前预估 + 生成中累计；到提醒线告警，到上限熔断。"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .config import Settings


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Estimate:
    images_cny: float
    videos_cny: float
    script_cny: float
    total_usd: float
    priced: bool

    def lines(self) -> list[str]:
        out = [
            f"  场景图   ¥{self.images_cny:,.2f}",
            f"  镜头视频 ¥{self.videos_cny:,.2f}",
            f"  脚本     ¥{self.script_cny:,.2f}",
            f"  合计约   ${self.total_usd:,.2f}（含重生成余量）",
        ]
        if not self.priced:
            out.append("  ⚠ 未配置 PRICE_SEEDREAM_PER_IMAGE / PRICE_SEEDANCE_PER_SECOND，预估偏低、不可靠")
        return out


def estimate(settings: Settings, n_shots: int, total_seconds: float,
             include_images: bool = True, include_videos: bool = True) -> Estimate:
    p = settings.pricing
    m = 1 + settings.regen_margin
    images = n_shots * settings.candidates_per_shot * p.seedream_per_image * m if include_images else 0.0
    videos = total_seconds * p.seedance_per_second * m if include_videos else 0.0
    script = 0.05  # DeepSeek 单次脚本通常只有几分钱，按固定值预留
    return Estimate(images, videos, script, p.cny_to_usd(images + videos + script), p.configured)


class Ledger:
    """记账簿，条目保存在项目 state 里（list[dict]），便于事后用真实账单校准单价。"""

    def __init__(self, settings: Settings, entries: list[dict]):
        self.settings = settings
        self.entries = entries
        self.warned = self.spent_usd >= settings.budget_warn_usd

    @property
    def spent_cny(self) -> float:
        return sum(e["cost_cny"] for e in self.entries)

    @property
    def spent_usd(self) -> float:
        return self.settings.pricing.cny_to_usd(self.spent_cny)

    def ensure_room(self, next_cost_cny: float, what: str) -> None:
        """提交一个新任务前调用：超过上限则拒绝。"""
        after = self.settings.pricing.cny_to_usd(self.spent_cny + next_cost_cny)
        if after > self.settings.budget_usd:
            raise BudgetExceeded(
                f"{what} 会让本片花费达到 ${after:.2f}，超过上限 ${self.settings.budget_usd:.0f}，已暂停。"
                f" 当前已花 ${self.spent_usd:.2f}。")

    def record(self, kind: str, model: str, cost_cny: float, shot: int | None = None,
               usage: dict | None = None, note: str = "") -> list[str]:
        self.entries.append({
            "kind": kind, "model": model, "shot": shot, "cost_cny": round(cost_cny, 4),
            "usage": usage or {}, "note": note, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        msgs = []
        if not self.warned and self.spent_usd >= self.settings.budget_warn_usd:
            self.warned = True
            msgs.append(f"⚠ 本片花费已达 ${self.spent_usd:.2f}，超过提醒线 ${self.settings.budget_warn_usd:.0f}")
        return msgs

    def summary(self) -> dict:
        by_kind: dict[str, float] = {}
        for e in self.entries:
            by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + e["cost_cny"]
        return {"spent_cny": round(self.spent_cny, 2), "spent_usd": round(self.spent_usd, 2),
                "by_kind_cny": {k: round(v, 2) for k, v in by_kind.items()},
                "budget_usd": self.settings.budget_usd}
