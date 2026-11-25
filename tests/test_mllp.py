import asyncio
from pathlib import Path

from hl7search.mllp import END_BLOCK, START_BLOCK, MLLPServer
from hl7search.storage import Store


def test_mllp_returns_application_accept(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = Store(tmp_path / "mllp.db")
        receiver = MLLPServer(store)
        server = await asyncio.start_server(receiver.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        raw = (Path(__file__).parents[1] / "examples" / "adt_a01_alice.hl7").read_bytes()
        writer.write(START_BLOCK + raw + END_BLOCK)
        await writer.drain()
        ack = await reader.readuntil(END_BLOCK)
        assert b"MSA|AA|ADT-001" in ack
        assert store.stats()["messages"] == 1
        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()
    asyncio.run(scenario())

