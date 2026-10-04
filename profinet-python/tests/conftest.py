from __future__ import annotations

import pytest
from helpers import mock_module


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def mock_server():
    """The mock IO device running in this event loop on an ephemeral port."""
    mod = mock_module()
    server = mod.ProfinetMockServer(mod.MockConfig(host="127.0.0.1", port=0, update_interval=3600))
    await server.start()
    try:
        yield server
    finally:
        await server.stop()
