from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import wave

import cv2
import numpy as np


# 用户提供的聊天栏「【狩猎验证】」字形，仅保存二值文字特征，
# 不保存聊天内容/角色画面，不读取验证题目、答案或操作任何输入通道。
KEYWORD = "【狩猎验证】"
_ROWS = (
    "000000000100010000000100100100000000001000010000000000000000",
    "111110010100001000010100100100011100001000001011111111000111",
    "111100001001111111001001111110000100010100000000001000000011",
    "111000010101000001010100100100010100100010000000001000000001",
    "110000000100000100000111111111010101000001011000001000000000",
    "110000000101111111000100000000010100111110001001001000000000",
    "110000001100000100001101111110011110000000001001001111000000",
    "110000010100100100010101000010000010010010001001001000000000",
    "111000000100010100000101111110000111001010001011001000000001",
    "111100000100000100000101000010011010101010001101001000000011",
    "111110000100000100000101111110000010100100001001001000000111",
    "000000011000011100011001000010001101111111000011111111000000",
)
KEYWORD_MASK = np.array([[int(c) for c in row] for row in _ROWS], dtype=np.uint8)
LEGACY_REGION = {"x": 0.0, "y": 0.65, "w": 0.5, "h": 0.27}
DEFAULT_REGION = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
SCAN_SECONDS = 0.5
CONFIRM_SCANS = 3
CLEAR_SECONDS = 10.0
COOLDOWN_SECONDS = 60.0
ALERT_SOUND_SECONDS = 10
ALERT_SAMPLE_RATE = 22050


def normalize_alert_settings(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    region = raw.get("chat_region", {})
    try:
        region = {key: float(region[key]) for key in DEFAULT_REGION}
        if not all(math.isfinite(n) for n in region.values()):
            raise ValueError
        if not (0 <= region["x"] < 1 and 0 <= region["y"] < 1
                and 0 < region["w"] <= 1 - region["x"] + 1e-9
                and 0 < region["h"] <= 1 - region["y"] + 1e-9):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        region = dict(DEFAULT_REGION)
    if region == LEGACY_REGION:
        # 旧示例区域自动升级；其他用户自定义比例区域仍保留。
        region = dict(DEFAULT_REGION)
    return {"enabled": raw.get("enabled") is True, "chat_region": region,
            "pause_on_detect": raw.get("pause_on_detect") is True}


@dataclass(frozen=True)
class KeywordMatch:
    score: float
    box: tuple[int, int, int, int]


@lru_cache(maxsize=16)
def _keyword_templates(width: int, height: int) -> tuple[np.ndarray, ...]:
    # 分辨率改变不一定缩放字体：始终保留原生及常见 DPI 字形，追加当前画面的等比倍率。
    scale = min(width / 1280.0, height / 720.0)
    scales = (1.0, 0.8, 1.25, 1.5, 2.0, max(0.5, min(4.0, scale)))
    templates = {}
    for factor in scales:
        template = cv2.resize(KEYWORD_MASK, None, fx=factor, fy=factor, interpolation=cv2.INTER_NEAREST)
        templates.setdefault(template.shape, template)
    return tuple(templates.values())


def keyword_match(frame: np.ndarray, region: dict[str, float]) -> KeywordMatch | None:
    """在当前游戏客户区（或自定义比例区域）查找字形，不读题目、不截桌面。"""
    height, width = frame.shape[:2]
    x, y = int(width * region["x"]), int(height * region["y"])
    right = min(width, int(width * (region["x"] + region["w"])))
    bottom = min(height, int(height * (region["y"] + region["h"])))
    roi = frame[y:bottom, x:right, :3].astype(np.int16)
    if roi.size == 0:
        return None
    blue, green, red = cv2.split(roi)
    mask = ((red - green > 35) & (np.abs(green - blue) < 30) & (green > 120)).astype(np.uint8)
    if np.count_nonzero(mask) < 30:
        return None
    best = None
    # 同时兼容原生字体及常见缩放；模板很小，避免引入 OCR 模型和下载依赖。
    for template in _keyword_templates(width, height):
        th, tw = template.shape
        if mask.shape[0] < th or mask.shape[1] < tw:
            continue
        scores = cv2.matchTemplate(mask, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(scores)
        if math.isfinite(score) and score >= 0.88 and (best is None or score > best.score):
            best = KeywordMatch(score, (x + location[0], y + location[1], tw, th))
    return best


class VerificationAlert:
    """纯观察器：去抖、限频、旧消息去重；不持有 bot/键盘/窗口控制对象。"""

    def __init__(self) -> None:
        self.next_scan = 0.0
        self.last_scan: float | None = None
        self.hits = 0
        self.latched = False
        self.missing_since: float | None = None
        self.last_alert = -math.inf
        self.settings: dict[str, Any] | None = None
        self.failed = False
        self.frame_size: tuple[int, int] | None = None

    def observe(self, frame: np.ndarray, raw: Any, now: float) -> KeywordMatch | None:
        settings = normalize_alert_settings(raw)
        if settings != self.settings:
            self.__init__()
            self.settings = settings
        if not settings["enabled"] or self.failed or now < self.next_scan:
            return None
        frame_size = frame.shape[:2]
        if frame_size != self.frame_size:
            self.hits = 0
            self.missing_since = None
            self.frame_size = frame_size
            # 不清除旧消息锁存和提醒冷却，避免调整窗口就重复响铃。
        # 截图停更、挂起、重新连接不能被当作连续命中或连续无提示。
        if self.last_scan is not None and now - self.last_scan > SCAN_SECONDS * 3:
            self.hits = 0
            self.missing_since = None
        self.last_scan = now
        self.next_scan = now + SCAN_SECONDS
        match = keyword_match(frame, settings["chat_region"])
        if match is None:
            self.hits = 0
            if self.missing_since is None:
                self.missing_since = now
            if now - self.missing_since >= CLEAR_SECONDS:
                self.latched = False
            return None
        self.missing_since = None
        self.hits += 1
        if self.hits < CONFIRM_SCANS or self.latched or now - self.last_alert < COOLDOWN_SECONDS:
            return None
        self.latched = True
        self.last_alert = now
        return match


@lru_cache(maxsize=1)
def _alert_sound_file() -> tuple[TemporaryDirectory, Path]:
    """一次生成有限长度双音 WAV；缓存保留临时目录，退出时清理。"""
    samples = np.zeros(ALERT_SAMPLE_RATE * ALERT_SOUND_SECONDS, dtype=np.float32)
    tone_length = int(ALERT_SAMPLE_RATE * 0.32)
    phase = np.arange(tone_length, dtype=np.float32) / ALERT_SAMPLE_RATE
    # 淡入淡出消除突然截断的爆音；不修改系统音量。
    envelope = np.ones(tone_length, dtype=np.float32)
    fade = int(ALERT_SAMPLE_RATE * 0.005)
    envelope[:fade] = np.linspace(0, 1, fade)
    envelope[-fade:] = np.linspace(1, 0, fade)
    for second in range(ALERT_SOUND_SECONDS):
        for offset, frequency in ((0.0, 880), (0.32, 1320)):
            start = round((second + offset) * ALERT_SAMPLE_RATE)
            samples[start:start + tone_length] = (
                0.65 * envelope * np.sin(2 * np.pi * frequency * phase)
            )
    directory = TemporaryDirectory(prefix="mbv-verification-sound-", ignore_cleanup_errors=True)
    path = Path(directory.name) / "verification-alert-10s.wav"
    try:
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(ALERT_SAMPLE_RATE)
            output.writeframes((samples * 32767).astype("<i2").tobytes())
    except Exception:
        directory.cleanup()
        raise
    return directory, path


def play_alert_sound() -> None:
    """异步播放约 10 秒双音提醒；重复调用替换当前声音，不叠加、不无限循环。"""
    import winsound

    _directory, path = _alert_sound_file()
    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
