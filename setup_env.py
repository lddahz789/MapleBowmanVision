from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / "Scripts" / "python.exe"
REQUIREMENTS = ROOT / "requirements.txt"
MIN_VERSION = (3, 10)
PYTHON_INSTALLER_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def print_utf8(message):
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("gbk", "replace").decode("gbk"))


def python_version(executable):
    try:
        output = subprocess.check_output(
            [str(executable), "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
            stderr=subprocess.STDOUT,
        )
        parts = output.decode("utf-8", "replace").strip().split(".")
        return tuple(int(part) for part in parts[:3])
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def is_new_enough(executable):
    version = python_version(executable)
    return version is not None and version >= MIN_VERSION


def iter_python_candidates():
    seen = set()
    local_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    if local_root.is_dir():
        folders = sorted(local_root.iterdir(), reverse=True)
        for folder in folders:
            exe = folder / "python.exe"
            if exe.is_file():
                seen.add(str(exe).lower())
                yield exe
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    if program_files.is_dir():
        for folder in sorted(program_files.glob("Python*"), reverse=True):
            exe = folder / "python.exe"
            if exe.is_file() and str(exe).lower() not in seen:
                seen.add(str(exe).lower())
                yield exe
    for minor in ("3.14", "3.13", "3.12", "3.11", "3.10"):
        try:
            output = subprocess.check_output(
                ["py", "-" + minor, "-c", "import sys; print(sys.executable)"],
                stderr=subprocess.STDOUT,
            )
            exe = Path(output.decode("utf-8", "replace").strip())
            if exe.is_file() and str(exe).lower() not in seen:
                seen.add(str(exe).lower())
                yield exe
        except (OSError, subprocess.CalledProcessError):
            continue


def find_python():
    for candidate in iter_python_candidates():
        if is_new_enough(candidate):
            return candidate
    return None


def download_installer(destination):
    import urllib.request

    print_utf8("未找到 Python 3.10+，正在下载官方 Python 3.12.10……")
    request = urllib.request.Request(
        PYTHON_INSTALLER_URL,
        headers={"User-Agent": "MapleBowmanVision-setup"},
    )
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    size = destination.stat().st_size
    if size < 10 * 1024 * 1024:
        raise RuntimeError("Python 安装包下载不完整（%s 字节）" % size)


def install_python():
    installer = Path(os.environ.get("TEMP", str(ROOT))) / "python-3.12.10-amd64.exe"
    download_installer(installer)
    print_utf8("正在安装 Python 3.12（仅当前用户，不会替换系统里的 Python 3.7）……")
    subprocess.check_call(
        [
            str(installer),
            "/quiet",
            "InstallAllUsers=0",
            "PrependPath=1",
            "Include_launcher=1",
            "Include_pip=1",
            "Include_test=0",
            "SimpleInstall=1",
        ]
    )
    found = find_python()
    if found is None:
        raise RuntimeError("Python 3.12 安装完成，但没有找到 python.exe。请关闭窗口后重新运行 Setup.bat。")
    return found


def recreate_venv(python_exe):
    if VENV.exists():
        print_utf8("正在删除旧的虚拟环境……")
        shutil.rmtree(VENV, ignore_errors=True)
    version = python_version(python_exe)
    print_utf8("正在用 Python %s 创建虚拟环境……" % ".".join(str(part) for part in version))
    subprocess.check_call([str(python_exe), "-m", "venv", str(VENV)])
    if not VENV_PYTHON.is_file():
        raise RuntimeError("虚拟环境创建失败。")


def pip_install():
    common = [
        str(VENV_PYTHON),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--timeout",
        "60",
        "--retries",
        "3",
    ]
    print_utf8("正在通过清华 PyPI 镜像安装依赖……")
    mirror = subprocess.call(common + ["-i", MIRROR, "-r", str(REQUIREMENTS)])
    if mirror == 0:
        return
    print_utf8("清华镜像安装失败，正在改用默认软件源重试……")
    fallback = subprocess.call(common + ["-r", str(REQUIREMENTS)])
    if fallback != 0:
        raise RuntimeError("依赖安装失败。")


def main():
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    print_utf8("正在检查 Python 和 pip……")
    python_exe = find_python()
    if python_exe is None:
        python_exe = install_python()
    print_utf8("将使用 Python %s（%s）" % (".".join(str(part) for part in python_version(python_exe)), python_exe))

    if not is_new_enough(VENV_PYTHON):
        recreate_venv(python_exe)
    else:
        print_utf8("已有可用的虚拟环境，跳过重建。")

    subprocess.check_call([str(VENV_PYTHON), "-m", "pip", "--version"])
    pip_install()
    print_utf8("安装完成。下一步请运行唯一入口 Start.bat，并在控制面板依次采集状态栏/小地图和识别区域/平台中心。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print_utf8("安装失败：%s" % exc)
        raise SystemExit(1)
