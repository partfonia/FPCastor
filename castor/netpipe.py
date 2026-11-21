"""Соединение «через прокси» для ssh (ProxyCommand): python netpipe.py <host> <port>.

Прокси берётся из переменной окружения CASTOR_PIPE_PROXY (http://, socks5://, socks5h://, с логином или без).
Скрипт подключается к host:port через прокси и гоняет байты между ssh и сокетом.
Без сторонних библиотек, чтобы работал в любом Python.
"""
from __future__ import annotations

import base64
import os
import socket
import struct
import sys
import threading
from urllib.parse import unquote, urlsplit


def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("прокси закрыл соединение")
        buf += chunk
    return buf


def open_via_proxy(proxy: str, host: str, port: int, timeout: float = 20) -> socket.socket:
    u = urlsplit(proxy)
    scheme = (u.scheme or "http").lower()
    s = socket.create_connection((u.hostname, u.port or (1080 if scheme.startswith("socks") else 8080)), timeout)
    user, pwd = unquote(u.username or ""), unquote(u.password or "")
    if scheme.startswith("socks5"):
        methods = b"\x00\x02" if user else b"\x00"
        s.sendall(b"\x05" + bytes([len(methods)]) + methods)
        ver, method = _recv_exact(s, 2)
        if method == 0x02:
            ub, pb = user.encode(), pwd.encode()
            s.sendall(b"\x01" + bytes([len(ub)]) + ub + bytes([len(pb)]) + pb)
            if _recv_exact(s, 2)[1] != 0:
                raise ConnectionError("неверный логин или пароль прокси")
        elif method != 0x00:
            raise ConnectionError("прокси не принял способ входа")
        hb = host.encode()
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", port))
        head = _recv_exact(s, 4)
        if head[1] != 0:
            raise ConnectionError(f"прокси не смог подключиться к {host}:{port} (код {head[1]})")
        alen = {1: 4, 4: 16}.get(head[3])
        _recv_exact(s, (alen if alen else _recv_exact(s, 1)[0]) + 2)
    else:  # HTTP CONNECT
        req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        if user:
            req += "Proxy-Authorization: Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode() + "\r\n"
        s.sendall((req + "\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("прокси закрыл соединение")
            resp += chunk
        code = resp.split(b" ", 2)[1] if b" " in resp else b"?"
        if code != b"200":
            raise ConnectionError(f"прокси ответил {code.decode(errors='replace')}")
    s.settimeout(None)
    return s


def main():
    if len(sys.argv) < 3:
        sys.stderr.write("usage: netpipe.py host port\n")
        return 2
    host, port = sys.argv[1], int(sys.argv[2])
    proxy = os.environ.get("CASTOR_PIPE_PROXY", "")
    try:
        s = open_via_proxy(proxy, host, port) if proxy else socket.create_connection((host, port), 20)
    except Exception as e:  # ssh покажет это в своём выводе
        sys.stderr.write(f"netpipe: {e}\n")
        return 1

    def up():
        try:
            while True:
                data = os.read(0, 65536)
                if not data:
                    break
                s.sendall(data)
        except Exception:
            pass
        try:
            s.shutdown(socket.SHUT_WR)
        except Exception:
            pass

    threading.Thread(target=up, daemon=True).start()
    try:
        while True:
            data = s.recv(65536)
            if not data:
                break
            os.write(1, data)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
