from __future__ import annotations

import ctypes
from ctypes import wintypes
import multiprocessing as mp
import time
from multiprocessing.connection import Connection

import numpy as np

from mbv.win32 import user32
from mbv.window import WindowInfo, client_window


class BackgroundCaptureError(OSError):
    pass


class BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("width", wintypes.LONG),
        ("height", wintypes.LONG), ("planes", wintypes.WORD),
        ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
        ("image_size", wintypes.DWORD), ("x_ppm", wintypes.LONG),
        ("y_ppm", wintypes.LONG), ("colors", wintypes.DWORD),
        ("important_colors", wintypes.DWORD),
    ]


def print_window_frame(hwnd: int) -> np.ndarray:
    """在隔离进程内调用；PrintWindow 本身没有超时参数。"""
    if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        raise BackgroundCaptureError("游戏窗口已关闭或最小化，请恢复窗口")
    window = client_window(hwnd, "")
    width, height = window.width, window.height
    if width * height > 16_777_216:
        raise BackgroundCaptureError("窗口截图尺寸超出支持范围")
    gdi = ctypes.windll.gdi32
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(BitmapInfoHeader), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
    ]
    gdi.CreateDIBSection.restype = wintypes.HBITMAP
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi.SelectObject.restype = wintypes.HANDLE
    gdi.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    source_dc = user32.GetDC(hwnd)
    memory_dc = None
    bitmap = None
    previous = None
    try:
        if not source_dc:
            raise BackgroundCaptureError("无法取得游戏窗口绘图句柄")
        memory_dc = gdi.CreateCompatibleDC(source_dc)
        if not memory_dc:
            raise BackgroundCaptureError("无法建立窗口截图缓冲区")
        header = BitmapInfoHeader()
        header.size = ctypes.sizeof(header)
        header.width, header.height = width, -height
        header.planes, header.bits = 1, 32
        bits = ctypes.c_void_p()
        bitmap = gdi.CreateDIBSection(memory_dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            raise BackgroundCaptureError("无法分配窗口截图位图")
        previous = gdi.SelectObject(memory_dc, bitmap)
        if not previous or previous == ctypes.c_void_p(-1).value:
            previous = None
            raise BackgroundCaptureError("无法选择窗口截图位图")
        ctypes.memset(bits, 0, width * height * 4)
        # PW_CLIENTONLY | PW_RENDERFULLCONTENT：坐标与原客户区校准一致。
        if not user32.PrintWindow(hwnd, memory_dc, 3):
            raise BackgroundCaptureError("客户端不支持窗口截图，或权限不足")
        gdi.GdiFlush()
        pixels = np.frombuffer(ctypes.string_at(bits, width * height * 4), dtype=np.uint8)
        frame = np.ascontiguousarray(pixels.reshape(height, width, 4)[:, :, :3])
        if float(frame.std()) < 1.0:
            raise BackgroundCaptureError("窗口截图为空白，客户端可能不支持后台绘制")
        return frame
    finally:
        if previous:
            gdi.SelectObject(memory_dc, previous)
        if bitmap:
            gdi.DeleteObject(bitmap)
        if memory_dc:
            gdi.DeleteDC(memory_dc)
        if source_dc:
            user32.ReleaseDC(hwnd, source_dc)


def _capture_worker(connection: Connection) -> None:
    try:
        connection.send((True, "ready"))
        while True:
            hwnd = connection.recv()
            if hwnd is None:
                return
            try:
                connection.send((True, print_window_frame(int(hwnd))))
            except Exception as exc:
                connection.send((False, str(exc)))
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()


class BackgroundCapture:
    """有时间上限的窗口截图；失败时禁止回退到被其他窗口遮挡的桌面。"""

    def __init__(self, timeout: float = 0.8) -> None:
        self.timeout = timeout
        self._process = None
        self._connection = None
        self._retry_at = 0.0

    def _start(self) -> None:
        context = mp.get_context("spawn")
        parent, child = context.Pipe()
        self._connection = parent
        self._process = context.Process(target=_capture_worker, args=(child,), daemon=True)
        try:
            self._process.start()
            child.close()
            if not parent.poll(5.0) or parent.recv() != (True, "ready"):
                raise BackgroundCaptureError("窗口截图辅助进程启动超时")
        except Exception:
            child.close()
            self.close()
            raise

    def capture(self, window: WindowInfo) -> np.ndarray:
        if time.monotonic() < self._retry_at:
            raise BackgroundCaptureError("窗口截图尚不可用，稍后自动重试")
        try:
            if self._process is None:
                self._start()
            self._connection.send(window.hwnd)
            if not self._connection.poll(self.timeout):
                raise BackgroundCaptureError("窗口截图超时，已停止本次截图")
            success, value = self._connection.recv()
            if not success:
                raise BackgroundCaptureError(value)
            return value
        except (EOFError, OSError) as exc:
            self.close()
            self._retry_at = time.monotonic() + 2.0
            raise BackgroundCaptureError(str(exc)) from exc

    def close(self) -> None:
        self._retry_at = 0.0
        process, connection = self._process, self._connection
        self._process = self._connection = None
        if connection is not None:
            connection.close()
        if process is not None and process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.3)
            if not process.is_alive():
                process.close()
