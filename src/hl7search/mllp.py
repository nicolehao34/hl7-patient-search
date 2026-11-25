from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from .parser import HL7ParseError, parse_message
from .storage import DuplicateMessageError, Store

START_BLOCK, END_BLOCK = b"\x0b", b"\x1c\x0d"
logger = logging.getLogger(__name__)


def make_ack(raw: bytes, code: str, text: str = "") -> bytes:
    try:
        msg = parse_message(raw)
        trigger, control, version = msg.trigger_event, msg.control_id, msg.version or "2.5.1"
        destination = f"{msg.sending_application}|{msg.sending_facility}"
    except HL7ParseError:
        trigger, control, version, destination = "U01", "UNKNOWN", "2.5.1", "|"
    now = datetime.now().strftime("%Y%m%d%H%M%S")
    ack = (f"MSH|^~\\&|HL7SEARCH|RECEIVER|{destination}|{now}||ACK^{trigger}^ACK|"
           f"ACK-{control}|P|{version}\rMSA|{code}|{control}|{text}\r")
    return START_BLOCK + ack.encode() + END_BLOCK


class MLLPServer:
    def __init__(self, store: Store, host: str = "0.0.0.0", port: int = 2575,
                 max_message_bytes: int = 5_000_000) -> None:
        self.store, self.host, self.port, self.max_message_bytes = store, host, port, max_message_bytes

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            while True:
                try:
                    await reader.readuntil(START_BLOCK)
                    framed = await reader.readuntil(END_BLOCK)
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                    break
                raw = framed[:-2]
                if len(raw) > self.max_message_bytes:
                    writer.write(make_ack(raw, "AR", "message exceeds configured size"))
                else:
                    try:
                        message = parse_message(raw)
                        self.store.ingest(message)
                        writer.write(make_ack(raw, "AA", "accepted"))
                    except DuplicateMessageError:
                        # Retries are idempotent: already-durable messages are accepted.
                        writer.write(make_ack(raw, "AA", "duplicate already accepted"))
                    except HL7ParseError as exc:
                        writer.write(make_ack(raw, "AE", str(exc)))
                    except Exception:
                        logger.exception("application rejection for %s", peer)
                        writer.write(make_ack(raw, "AR", "internal processing error"))
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def serve(self) -> None:
        server = await asyncio.start_server(self.handle, self.host, self.port,
                                            limit=self.max_message_bytes + 1024)
        logger.info("MLLP listening on %s:%s", self.host, self.port)
        async with server:
            await server.serve_forever()

