from __future__ import annotations

import ctypes
import os
import sys


if os.name == "nt":
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


MESSAGES = {
    "need_setup": "请先运行 Setup.bat 安装运行环境。",
    "setup_check": "正在检查 Python 和 pip……",
    "setup_install_mirror": "正在通过清华 PyPI 镜像安装依赖……",
    "setup_retry_default": "清华镜像安装失败，正在改用默认软件源重试……",
    "setup_success": "安装完成。下一步请运行 Start.bat，在控制面板里进行画面校准。",
    "setup_failed": "安装失败，请查看上方的错误信息。",
    "setup_need_python": "需要 Python 3.10 或更高版本。当前环境过旧，无法安装 numpy 2.1。",
    "input_authorized": "本次运行已允许按键输入，请保持游戏位于前台。",
    "hotkeys": "F7 显隐 Debug 框，F8 启动或暂停，F9 立即退出。",
    "close_window": "按任意键关闭窗口……",
}


def main() -> int:
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    message = MESSAGES.get(key)
    if message is None:
        print(f"未知提示编号：{key}")
        return 2
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
