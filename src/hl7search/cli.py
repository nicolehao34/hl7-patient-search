from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import threading
from pathlib import Path

import uvicorn

from .api import create_app
from .mllp import END_BLOCK, START_BLOCK, MLLPServer
from .storage import Store


def send_message(host: str, port: int, path: Path) -> None:
    raw = path.read_bytes().replace(b"\\r", b"\r")
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(START_BLOCK + raw.strip(b"\r\n") + END_BLOCK)
        response = b""
        while END_BLOCK not in response:
            chunk = sock.recv(65536)
            if not chunk: break
            response += chunk
    print(response.strip(START_BLOCK + END_BLOCK).decode())


def main() -> None:
    parser = argparse.ArgumentParser(prog="hl7-search")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="start MLLP receiver and HTTP API")
    serve.add_argument("--database", default="hl7.db")
    serve.add_argument("--mllp-host", default="0.0.0.0"); serve.add_argument("--mllp-port", type=int, default=2575)
    serve.add_argument("--http-host", default="127.0.0.1"); serve.add_argument("--http-port", type=int, default=8000)
    send = sub.add_parser("send", help="send an HL7 file over MLLP")
    send.add_argument("path", type=Path); send.add_argument("--host", default="127.0.0.1"); send.add_argument("--port", type=int, default=2575)
    args = parser.parse_args()
    if args.command == "send":
        send_message(args.host, args.port, args.path); return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = Store(args.database)
    thread = threading.Thread(target=lambda: asyncio.run(MLLPServer(store, args.mllp_host, args.mllp_port).serve()), daemon=True)
    thread.start()
    uvicorn.run(create_app(args.database), host=args.http_host, port=args.http_port)


if __name__ == "__main__":
    main()

