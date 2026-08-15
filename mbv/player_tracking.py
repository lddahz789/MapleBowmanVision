from __future__ import annotations

from dataclasses import dataclass
import math

from mbv.vision import PlayerAnchor


MINIMAP_STATIONARY_RADIUS_PIXELS = 2.0


def _anchor_point(anchor: PlayerAnchor) -> tuple[float, float]:
    anchor_x, feet_y, anchor_width, _anchor_height = anchor.box
    return anchor_x + anchor_width / 2.0, float(feet_y)


@dataclass(frozen=True)
class MinimapStationaryReference:
    """最近一次可靠视觉锚点与同帧唯一小地图标记的固定基准。"""

    anchor: PlayerAnchor
    marker: tuple[float, float]
    seen_at: float
    scene_size: tuple[int, int]
    minimap_size: tuple[int, int]


@dataclass(frozen=True)
class MinimapStationaryEvidence:
    """固定基准未发生可信位移的短时证据；不会刷新视觉存活时间。"""

    anchor: PlayerAnchor
    distance_pixels: float
    age_seconds: float


def _valid_marker(marker: tuple[float, float] | None) -> bool:
    if marker is None or len(marker) != 2:
        return False
    try:
        values = tuple(float(value) for value in marker)
    except (TypeError, ValueError):
        return False
    return all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in values)


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
    pending_kind: str = ""
    nameplate_identity_established: bool = False
    minimap_stationary_reference: MinimapStationaryReference | None = None
    minimap_stationary_evidence: MinimapStationaryEvidence | None = None
    minimap_stationary_blocked: bool = False

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
        # 任一真实视觉锚点都结束上一段遮挡；静止失效门禁不能跨视觉重获延续。
        self.minimap_stationary_blocked = False
        if identity_confirmed:
            self.mode = "LOCKED"
            self.last_identity_at = now
            if anchor.source == "姓名板":
                self.nameplate_identity_established = True
            self.pending_anchor = None
            self.pending_count = 0
            self.pending_kind = ""
        else:
            self.mode = "OCCLUDED"

    def has_confirmed_identity(self) -> bool:
        """是否已由姓名板或连续高置信辅助模板确认视觉身份。"""
        return self.last_identity_at > 0.0

    def has_nameplate_identity(self) -> bool:
        """是否曾由本人姓名板建立身份；辅助模板不能满足这个门槛。"""
        return self.nameplate_identity_established

    def clear_minimap_evidence(self) -> None:
        self.minimap_stationary_evidence = None

    def invalidate_minimap_assist(self, *, block_hold: bool = False) -> None:
        self.minimap_stationary_reference = None
        self.minimap_stationary_evidence = None
        self.minimap_stationary_blocked = bool(block_hold)

    def record_minimap_stationary_reference(
        self,
        anchor: PlayerAnchor,
        marker: tuple[float, float] | None,
        now: float,
        scene_width: int,
        scene_height: int,
        minimap_width: int,
        minimap_height: int,
        *,
        head_continuous: bool,
        marker_unambiguous: bool,
    ) -> bool:
        """记录固定静止基准；只接受姓名板或其后连续跟踪到的头部。"""
        source = anchor.source.split("（", 1)[0]
        eligible_source = source == "姓名板" or (source == "头部" and head_continuous)
        if not self.has_nameplate_identity() or not eligible_source:
            return False
        if not marker_unambiguous or not _valid_marker(marker):
            self.invalidate_minimap_assist()
            return False
        width = int(scene_width)
        height = int(scene_height)
        map_width = int(minimap_width)
        map_height = int(minimap_height)
        point = _anchor_point(anchor)
        if (
            width <= 0
            or height <= 0
            or map_width <= 0
            or map_height <= 0
            or not math.isfinite(float(now))
            or not all(math.isfinite(value) for value in point)
            or not (0.0 <= point[0] < width and 0.0 <= point[1] < height)
        ):
            return False
        self.minimap_stationary_reference = MinimapStationaryReference(
            anchor=anchor,
            marker=(float(marker[0]), float(marker[1])),
            seen_at=float(now),
            scene_size=(width, height),
            minimap_size=(map_width, map_height),
        )
        self.minimap_stationary_evidence = None
        self.minimap_stationary_blocked = False
        return True

    def minimap_stationary_anchor(
        self,
        marker: tuple[float, float] | None,
        now: float,
        scene_width: int,
        scene_height: int,
        minimap_width: int,
        minimap_height: int,
        *,
        max_seconds: float,
        marker_unambiguous: bool,
    ) -> PlayerAnchor | None:
        """小地图近似静止时短时沿用旧视觉锚点，不滚动刷新任何时间。"""
        self.minimap_stationary_evidence = None
        if not marker_unambiguous or not _valid_marker(marker):
            self.invalidate_minimap_assist(block_hold=True)
            return None
        reference = self.minimap_stationary_reference
        if reference is None:
            return None
        width = int(scene_width)
        height = int(scene_height)
        map_width = int(minimap_width)
        map_height = int(minimap_height)
        if (
            reference.scene_size != (width, height)
            or reference.minimap_size != (map_width, map_height)
            or width <= 0
            or height <= 0
            or map_width <= 0
            or map_height <= 0
            or not math.isfinite(float(now))
        ):
            self.invalidate_minimap_assist(block_hold=True)
            return None
        age = float(now) - reference.seen_at
        limit = float(max_seconds)
        if not math.isfinite(limit) or limit < 0.0:
            return None
        if age < 0.0 or age > limit + 1e-9:
            self.invalidate_minimap_assist(block_hold=True)
            return None
        current_marker = (float(marker[0]), float(marker[1]))
        delta_pixels = (
            (current_marker[0] - reference.marker[0]) * map_width,
            (current_marker[1] - reference.marker[1]) * map_height,
        )
        distance_pixels = math.hypot(*delta_pixels)
        if distance_pixels > MINIMAP_STATIONARY_RADIUS_PIXELS + 1e-9:
            # 一旦观察到可信移动，旧基准永久失效；即使随后走回原点也不能复活。
            self.invalidate_minimap_assist(block_hold=True)
            return None
        anchor = PlayerAnchor(
            reference.anchor.box,
            -1.0,
            "小地图静止确认",
            reference.anchor.raw_box,
        )
        self.minimap_stationary_evidence = MinimapStationaryEvidence(
            anchor=anchor,
            distance_pixels=distance_pixels,
            age_seconds=age,
        )
        return anchor

    def consider_reacquisition(
        self,
        anchor: PlayerAnchor,
        required_frames: int,
        max_distance: float,
        *,
        kind: str = "",
    ) -> PlayerAnchor | None:
        required = max(1, int(required_frames))
        if required <= 1:
            self.pending_anchor = None
            self.pending_count = 0
            self.pending_kind = ""
            return anchor
        selected_kind = str(kind)
        if self.pending_count > 0 and self.pending_kind != selected_kind:
            self.pending_anchor = None
            self.pending_count = 0
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
        self.pending_kind = selected_kind
        self.mode = "REACQUIRE"
        if self.pending_count < required:
            return None
        confirmed = self.pending_anchor
        self.pending_anchor = None
        self.pending_count = 0
        self.pending_kind = ""
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
        self.nameplate_identity_established = False
        self.pending_anchor = None
        self.pending_count = 0
        self.pending_kind = ""
        self.invalidate_minimap_assist()
