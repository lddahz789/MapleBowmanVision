from __future__ import annotations

from dataclasses import dataclass

from mbv.vision import PlayerAnchor


def _anchor_point(anchor: PlayerAnchor) -> tuple[float, float]:
    anchor_x, feet_y, anchor_width, _anchor_height = anchor.box
    return anchor_x + anchor_width / 2.0, float(feet_y)


@dataclass
class PlayerTrackState:
    """玩家定位的时序状态；只有真实检测才能刷新位置和存活时间。"""

    anchor: PlayerAnchor | None = None
    last_seen_at: float = 0.0
    last_auxiliary_at: float = 0.0
    last_global_at: float = 0.0
    misses: int = 0
    velocity: tuple[float, float] = (0.0, 0.0)
    mode: str = "SEARCH_SELF"
    last_identity_at: float = 0.0
    pending_anchor: PlayerAnchor | None = None
    pending_count: int = 0

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
        identity_confirmed: bool = True,
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
        if identity_confirmed:
            self.mode = "LOCKED"
            self.last_identity_at = now
            self.pending_anchor = None
            self.pending_count = 0
        else:
            self.mode = "OCCLUDED"

    def has_confirmed_identity(self) -> bool:
        """是否已由姓名板或连续高置信辅助模板确认视觉身份。"""
        return self.last_identity_at > 0.0

    def consider_reacquisition(
        self,
        anchor: PlayerAnchor,
        required_frames: int,
        max_distance: float,
    ) -> PlayerAnchor | None:
        required = max(1, int(required_frames))
        if required <= 1:
            self.pending_anchor = None
            self.pending_count = 0
            return anchor
        current_x, current_y = _anchor_point(anchor)
        same_candidate = False
        if self.pending_anchor is not None:
            previous_x, previous_y = _anchor_point(self.pending_anchor)
            distance = ((current_x - previous_x) ** 2 + (current_y - previous_y) ** 2) ** 0.5
            same_candidate = distance <= max(4.0, float(max_distance))
        if same_candidate:
            self.pending_count += 1
        else:
            self.pending_count = 1
        self.pending_anchor = anchor
        self.mode = "REACQUIRE"
        if self.pending_count < required:
            return None
        confirmed = self.pending_anchor
        self.pending_anchor = None
        self.pending_count = 0
        return confirmed

    def mark_miss(self) -> None:
        self.misses += 1
        if self.pending_count > 0:
            self.mode = "REACQUIRE"
        else:
            self.mode = "OCCLUDED" if self.anchor is not None else "SEARCH_SELF"

    def reset(self) -> None:
        self.anchor = None
        self.last_seen_at = 0.0
        self.last_auxiliary_at = 0.0
        self.last_global_at = 0.0
        self.misses = 0
        self.velocity = (0.0, 0.0)
        self.mode = "SEARCH_SELF"
        self.last_identity_at = 0.0
        self.pending_anchor = None
        self.pending_count = 0
