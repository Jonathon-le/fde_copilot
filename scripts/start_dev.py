"""一键启动开发环境：API + Streamlit，并打开浏览器。

用法（在项目根目录）：
    python -m scripts.start_dev
    python -m scripts.start_dev --api-port 8001 --ui-port 8502
    python -m scripts.start_dev --dry-run        # 只打印将要执行的命令

为什么还要一个脚本：API 和界面是两个常驻进程，每次手动开两个终端、切目录、
设变量，容易漏。脚本只做"检查 + 拉起"，不隐藏进程——两个新窗口都能看到日志，
出问题仍然能像以前一样排查。

注意：
- 不会自动设置 LLM_API_KEY。没设置时只警告，因为纯工单/界面浏览不需要模型。
- 首次使用前的一次性步骤（build_index、init-db）不会自动执行，
  缺东西时脚本会提示该跑哪条命令。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CREATE_NEW_CONSOLE = 0x00000010  # Windows: 给子进程开独立窗口


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="start_dev", description="启动 API 与界面")
    p.add_argument("--api-port", type=int, default=8000)
    p.add_argument("--ui-port", type=int, default=8501)
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.add_argument("--no-ui", action="store_true", help="只启动 API")
    p.add_argument("--dry-run", action="store_true", help="只打印命令")
    return p


def warn(msg: str) -> None:
    print(f"[提示] {msg}")


def port_busy(port: int) -> bool:
    """尝试连一下端口：能连上就说明已有服务在跑。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
            return True
    except Exception:
        return False


def wait_health(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not os.getenv("LLM_API_KEY", "").strip():
        warn("没有设置 LLM_API_KEY：问答/Agent 会返回 502；只浏览工单和界面不受影响。")
        warn('PowerShell 临时设置：$env:LLM_API_KEY="sk-..."；永久设置：setx LLM_API_KEY "sk-..."')
    if not (ROOT / "data" / "chroma").exists():
        warn("没有 data/chroma/：先运行 python -m scripts.build_index")
    if not (ROOT / "data" / "app.db").exists():
        warn("没有 data/app.db：先运行 python -m scripts.ticket_cli init-db")
    if port_busy(args.api_port):
        warn(f"端口 {args.api_port} 上已经有服务在跑，跳过启动 API。")
        api_proc = None
    else:
        api_cmd = [sys.executable, "-m", "uvicorn", "app.api.main:app",
                   "--host", "127.0.0.1", "--port", str(args.api_port), "--reload"]
        print("启动 API：", " ".join(api_cmd))
        api_proc = None if args.dry_run else subprocess.Popen(
            api_cmd, cwd=ROOT, creationflags=CREATE_NEW_CONSOLE
        )

    ui_proc = None
    if not args.no_ui:
        ui_cmd = [sys.executable, "-m", "streamlit", "run", "app/ui/streamlit_app.py",
                  "--server.port", str(args.ui_port)]
        print("启动界面：", " ".join(ui_cmd))
        if not args.dry_run:
            env = dict(os.environ, API_BASE_URL=f"http://127.0.0.1:{args.api_port}")
            ui_proc = subprocess.Popen(ui_cmd, cwd=ROOT, env=env,
                                       creationflags=CREATE_NEW_CONSOLE)

    if args.dry_run:
        print(f"\n（dry-run）API: http://127.0.0.1:{args.api_port}/docs  "
              f"界面: http://127.0.0.1:{args.ui_port}")
        return 0

    # 等 API 就绪再开浏览器，避免用户先看到"连不上"
    if api_proc is not None and not wait_health(args.api_port):
        warn("30 秒内没等到 /health，去看 API 窗口的报错。")
    print(f"\nAPI 文档：http://127.0.0.1:{args.api_port}/docs")
    print(f"界面    ：http://127.0.0.1:{args.ui_port}")
    print("关闭：直接关掉两个新开的窗口，或 Ctrl+C。")
    if not args.no_browser and not args.no_ui:
        webbrowser.open(f"http://127.0.0.1:{args.ui_port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
