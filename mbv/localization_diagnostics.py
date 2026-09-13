"""只读、限量的定位证据；压缩与磁盘写入不占用行动线程。"""
from __future__ import annotations

from collections import deque
from datetime import datetime
import json
from pathlib import Path
import queue
import threading
import time
import uuid
import zlib

import cv2


def anchor_record(anchor):
    if anchor is None:
        return None
    return {"box": anchor.box, "raw_box": anchor.raw_box,
            "score": anchor.score, "source": anchor.source}


def track_record(track, now):
    reference = track.minimap_stationary_reference
    return {
        "frame_index": track.frame_index, "mode": track.mode, "misses": track.misses,
        "anchor": anchor_record(track.anchor), "velocity": track.velocity,
        "visual_age": now - track.last_seen_at if track.last_seen_at > 0 else None,
        "navigation_age": now - track.minimap_navigation_seen_at
        if track.minimap_navigation_seen_at > 0 else None,
        "nameplate_identity": track.has_nameplate_identity(),
        "pending": {"anchor": anchor_record(track.pending_anchor), "count": track.pending_count,
                    "kind": track.pending_kind, "frame_index": track.pending_frame_index},
        "minimap_blocked": track.minimap_stationary_blocked,
        "head_recovery": {"hits": track.head_recovery.hits, "gaps": track.head_recovery.gaps,
                          "reason": track.head_recovery.reason},
        "minimap_reference": None if reference is None else {
            "marker": reference.marker, "age": now - reference.seen_at,
            "scene_size": reference.scene_size, "minimap_size": reference.minimap_size,
        },
    }


class LocalizationDiagnostics:
    """>=2 秒连续真实视觉缺失留样；暂停单独结案，不混入挂机时长。"""

    def __init__(self, session_path: Path, *, max_snapshots=60, max_bytes=256 * 1024 * 1024):
        self.root = session_path.parent / "localization"
        self.directory = self.root / (session_path.stem + "-" + uuid.uuid4().hex[:8])
        self.max_snapshots = max_snapshots
        self.max_bytes = max_bytes
        self._queue = queue.Queue(maxsize=4)
        self._stop = threading.Event()
        self._thread = None
        self._disabled = False
        self._notice = None
        self._snapshots = 0
        self._sequence = 0
        self._episode = 0
        self._lost_at = None
        self._first_failure = None
        self._reported = False
        self._last_report = 0.0
        self._history = deque(maxlen=32)
        self._healthy = None
        self._healthy_at = -float("inf")
        self._hash = None
        self._hash_since = 0.0
        self._dropped = 0

    @property
    def enabled(self):
        return not self._disabled and not self._stop.is_set()

    def take_notice(self):
        notice, self._notice = self._notice, None
        return notice

    def observe(self, frame, minimap, metadata, now, *, visual_ok, armed):
        if not self.enabled:
            return
        try:
            self._observe(frame, minimap, metadata, now, visual_ok=visual_ok, armed=armed)
        except Exception as exc:
            self._fail(exc)

    def _observe(self, frame, minimap, metadata, now, *, visual_ok, armed):
        digest = zlib.crc32(frame[::8, ::8].tobytes())
        if digest != self._hash:
            self._hash, self._hash_since = digest, now
        metadata = {**metadata, "ts": time.time(), "monotonic": now,
                    "local_time": datetime.now().isoformat(timespec="milliseconds"),
                    "armed": bool(armed), "sample_crc32": digest,
                    "sample_unchanged_seconds": max(0.0, now - self._hash_since)}
        self._history.append({k: metadata.get(k) for k in
                              ("monotonic", "state", "reason", "branch", "after", "scores")})
        if not armed:
            if self._reported:
                self._submit("interrupted", metadata, None, None)
            self._reset_loss()
            self._healthy = None
            return
        if visual_ok:
            if self._reported:
                self._submit("recovered", metadata, frame, minimap)
            self._reset_loss()
            if now - self._healthy_at >= 0.5 and frame.nbytes + minimap.nbytes <= 16 * 1024 * 1024:
                self._healthy = (frame.copy(), minimap.copy(), metadata)
                self._healthy_at = now
            return
        if self._lost_at is None:
            self._lost_at = now
            self._first_failure = metadata
            self._episode += 1
        age = now - self._lost_at
        if age < 2.0:
            return
        if not self._reported:
            if self._healthy is not None:
                healthy_frame, healthy_map, healthy_meta = self._healthy
                self._submit("before_loss", healthy_meta, healthy_frame, healthy_map)
            self._healthy = None
            self._submit("lost", metadata, frame, minimap)
            self._reported = True
            self._last_report = now
        elif now - self._last_report >= 5.0:
            # 每次事件只在 2/7/12 秒及恢复时留图；更久仍有每 5 秒文字。
            save_frame = age < 15.0
            self._submit("still_lost", metadata, frame if save_frame else None,
                         minimap if save_frame else None)
            self._last_report = now

    def _reset_loss(self):
        self._lost_at = None
        self._first_failure = None
        self._reported = False

    def _submit(self, phase, metadata, frame, minimap):
        if self._queue.full():
            self._dropped += 1
            return
        self._sequence += 1
        record = {**metadata, "schema": 1, "episode": self._episode, "phase": phase,
                  "lost_seconds": max(0.0, metadata["monotonic"] - self._lost_at)
                  if self._lost_at is not None else 0.0,
                  "recent_frames": list(self._history) if phase != "before_loss" else [],
                  "first_failure": self._first_failure if phase != "before_loss" else None,
                  "dropped_records": self._dropped}
        images = None
        record["image_status"] = "not_scheduled"
        if frame is not None and minimap is not None:
            record["image_status"] = "snapshot_limit"
            if self._snapshots < self.max_snapshots:
                record["image_status"] = "frame_too_large"
                if frame.nbytes + minimap.nbytes <= 16 * 1024 * 1024:
                    images = (frame.copy(), minimap.copy())
                    self._snapshots += 1
                    record["image_status"] = "saved"
        # 元数据在生产线程冻结；后续热更新、检测不能篡改排队中的证据。
        payload = json.dumps(record, ensure_ascii=False, allow_nan=False)
        self._queue.put_nowait((self._sequence, payload, images))
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="localization-diagnostics", daemon=True)
            self._thread.start()

    def _worker(self):
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            used = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    sequence, payload, images = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    files = {f"{sequence:04d}.json": payload.encode("utf-8")}
                    if images is not None:
                        for name, pixels in zip(("combat", "minimap"), images):
                            ok, encoded = cv2.imencode(".png", pixels)
                            if not ok:
                                raise OSError("定位留样 PNG 编码失败")
                            files[f"{sequence:04d}-{name}.png"] = encoded.tobytes()
                    size = sum(len(data) for data in files.values())
                    if used + size > self.max_bytes:
                        raise OSError("定位证据已达 256 MiB 总量上限；请归档 logs/localization 后重启")
                    self.directory.mkdir(parents=True, exist_ok=True)
                    # 先图片后 JSON；存在 JSON 才表示该组留样完成。
                    for name, data in sorted(files.items(), key=lambda item: item[0].endswith(".json")):
                        destination = self.directory / name
                        temporary = destination.with_suffix(destination.suffix + ".tmp")
                        temporary.write_bytes(data)
                        temporary.replace(destination)
                    used += size
                finally:
                    self._queue.task_done()
        except Exception as exc:
            self._fail(exc)

    def _fail(self, exc):
        self._disabled = True
        self._healthy = None
        self._notice = f"定位诊断已停用（不影响挂机）：{type(exc).__name__}: {exc}"
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
