# -*- coding: utf-8 -*-
"""开一条 SSH 隧道把生产 PostgreSQL 映射到本地 15432。

批量匹配要写生产库，但不能在服务器上跑浏览器（1688 要在本机网络出口），
所以本机跑脚本 + 隧道连库是最省事的组合。

用法：
    python tools/source_match/tunnel.py          # 前台保持
配合：
    set AOHUANG_DATABASE_URL=postgresql+psycopg://...@127.0.0.1:15432/miaoshou_listing_v2
"""
from __future__ import annotations

import pathlib
import select
import socket
import socketserver
import sys

import paramiko

HOST = "38.95.75.47"
USER = "root"
PW_FILE = pathlib.Path(r"C:\Users\Administrator\Desktop\雨云.txt")
LOCAL_PORT = 15432
REMOTE = ("127.0.0.1", 5432)


def read_pw() -> str:
    lines = [x.strip() for x in PW_FILE.read_text(encoding="utf-8").splitlines()
             if x.strip()]
    return lines[-1]


class Forward(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        chan = self.server.transport.open_channel(  # type: ignore[attr-defined]
            "direct-tcpip", REMOTE, self.request.getpeername())
        if chan is None:
            return
        while True:
            r, _, _ = select.select([self.request, chan], [], [])
            if self.request in r:
                data = self.request.recv(4096)
                if not data:
                    break
                chan.send(data)
            if chan in r:
                data = chan.recv(4096)
                if not data:
                    break
                self.request.send(data)
        chan.close()
        self.request.close()


def main() -> int:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, 22, USER, read_pw(), timeout=30,
                   allow_agent=False, look_for_keys=False)
    transport = client.get_transport()
    assert transport is not None
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.ThreadingTCPServer(("127.0.0.1", LOCAL_PORT), Forward)
    server.transport = transport  # type: ignore[attr-defined]
    print(f"隧道已开：127.0.0.1:{LOCAL_PORT} -> {HOST}:5432", flush=True)
    print("保持这个进程不要关。Ctrl+C 断开。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n断开")
    finally:
        server.shutdown()
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())