#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AS 交易工作台 · 一键启动器(替代 bat)
双击本文件 或 由「启动工作台.bat」调用均可。
逻辑: 清残留进程 → 起数据桥 → 开浏览器 → 等待
"""
import os
import sys
import time
import socket
import subprocess
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8899
URL = f"http://127.0.0.1:{PORT}/"


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill_port_procs(port):
    """清理占用端口的残留进程(跨平台)"""
    if os.name == "nt":
        cmd = f'netstat -ano | findstr ":{port}" | findstr "LISTENING"'
        try:
            out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10).stdout
            pids = set()
            for line in out.splitlines():
                parts = line.split()
                if parts and parts[-1].isdigit():
                    pids.add(parts[-1])
            for pid in pids:
                subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True, timeout=10)
                print(f"  清理残留进程 PID {pid}")
        except Exception:
            pass


def main():
    print("=" * 52)
    print("  AS 交易工作台 · 一键启动")
    print("=" * 52)
    print()
    print("[1/4] 检查端口占用...")
    kill_port_procs(PORT)
    time.sleep(1)

    print(f"[2/4] 启动数据桥 (http://127.0.0.1:{PORT}/)...")
    py = sys.executable
    proc = subprocess.Popen(
        [py, os.path.join(ROOT, "dashboard_server.py"), "--port", str(PORT)],
        cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
    )
    print(f"      数据桥 PID {proc.pid} (新窗口, 保持运行)")

    print("[3/4] 等待服务就绪...")
    for _ in range(15):
        time.sleep(1)
        if port_in_use(PORT):
            break

    print("[4/4] 打开工作台...")
    webbrowser.open(URL)

    print()
    print(f"  工作台: {URL}")
    print("  数据桥窗口请保持运行(最小化即可)。")
    print("  关闭本窗口不会影响数据桥。")
    print()
    input("按回车退出...")


if __name__ == "__main__":
    main()
