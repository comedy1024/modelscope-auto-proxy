"""
Windows 启动脚本 — 创建虚拟环境并启动服务
"""
import subprocess
import sys
import os
from pathlib import Path

ROOT = Path(__file__).parent
VENV = ROOT / ".venv"


def ensure_venv():
    """确保虚拟环境存在"""
    if not VENV.exists():
        print(f"创建虚拟环境: {VENV}")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)

    pip = VENV / "Scripts" / "pip.exe"
    if not pip.exists():
        pip = VENV / "bin" / "pip"

    print("安装依赖...")
    subprocess.run([str(pip), "install", "-r", str(ROOT / "requirements.txt")], check=True)


def start():
    """启动服务"""
    ensure_venv()

    python = VENV / "Scripts" / "python.exe"
    if not python.exists():
        python = VENV / "bin" / "python"

    os.chdir(str(ROOT))
    subprocess.run([str(python), "main.py"])


if __name__ == "__main__":
    start()
