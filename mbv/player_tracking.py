from __future__ import annotations

from dataclasses import dataclass

from mbv.vision import PlayerAnchor


def _anchor_point(anchor: PlayerAnchor) -> tuple[float, float]:
    raw_x, _raw_y, raw_width, _raw_height = anchor.raw_box
    return raw_x + raw_width / 2.0, float(anchor.box[1])


@dataclass
class PlayerTrackState:
    """玩家定位的时序状态；只有真实检测才能刷新位置和存活时间。"""

    anchor: PlayerAnchor | None = None
    last_seen_at: float = 0.0
    last_auxiliary_at: float = 0.0
    last_global_at: float = 0.0
    misses: int = 0
    velocity: tuple[float, float] = (0.0, 0.0)

    def anchor_within_hold(self, now: float, hold_seconds: float) -> PlayerAnchor | None:
        if self.anchor is not None and now - self.last_seen_at <= max(0.0, float(hold_seconds)):
            return self.anchor
        return None

    def predicted_point(self, now: float, horizon_seconds: float) -> tuple[float, float] | None:
        if self.anchor is None:
            return None
        center_x, feet_y = _anchor_point(self.anchor)
        elapsed = max(0.0, min(float(horizon_seconds), now - self.last_seen_at))
        return center_x + self.velocity[0] * elapsed, feet_y + self.velocity[1] * elapsed

    def needs_global_scan(self, now: float, interval_seconds: float, miss_limit: int) -> bool:
        if self.anchor is None or self.last_global_at <= 0.0:
            return True
        if self.misses >= max(1, int(miss_limit)):
            return True
        return now - self.last_global_at >= max(0.0, float(interval_seconds))

    def record(
        self,
        anchor: PlayerAnchor,
        now: float,
        *,
        velocity_alpha: float,
        max_displacement: float,
    ) -> None:
        previous = self.anchor
        previous_at = self.last_seen_at
        next_velocity = (0.0, 0.0)
        if previous is not None and previous_at > 0.0 and now > previous_at:
            previous_x, previous_y = _anchor_point(previous)
            current_x, current_y = _anchor_point(anchor)
            dx = current_x - previous_x
            dy = current_y - previous_y
            distance = (dx * dx + dy * dy) ** 0.5
            if previous.source == anchor.source and distance <= max(1.0, float(max_displacement)):
                elapsed = now - previous_at
                measured = (dx / elapsed, dy / elapsed)
                alpha = max(0.0, min(1.0, float(velocity_alpha)))
                next_velocity = (
                    self.velocity[0] + (measured[0] - self.velocity[0]) * alpha,
                    self.velocity[1] + (measured[1] - self.velocity[1]) * alpha,
                )
        self.anchor = anchor
        self.last_seen_at = now
        self.misses = 0
        self.velocity = next_velocity

    def mark_miss(self) -> None:
        self.misses += 1

    def reset(self) -> None:
        self.anchor = None
        self.last_seen_at = 0.0
        self.last_auxiliary_at = 0.0
        self.last_global_at = 0.0
        self.misses = 0
        self.velocity = (0.0, 0.0)
