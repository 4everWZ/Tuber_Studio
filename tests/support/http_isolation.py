"""HTTP transport mocks and JSON schema complexity checks for isolated tests."""

import httpx
import pytest_asyncio

from tuber_studio.infra.platforms.bilibili.browsing import BrowsingAdapter
from tuber_studio.infra.platforms.bilibili.client import BilibiliAdapter


def validate_schema_complexity(schema):
    # Schemas can describe ordinary URLs as data, but cannot acquire a resolver
    # capable of reading URLs/files. Bound nesting before the native converter.
    pending = [(schema, 0)]
    visited = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if depth > 32 or visited > 4096:
            raise ValueError("Response schema exceeds the supported complexity")
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef", "$id"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise ValueError("Response schemas support local references only")
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)


@pytest_asyncio.fixture
async def adapter_factory():
    adapters = []

    async def create(handler, credentials=None):
        adapter = BilibiliAdapter(credentials)
        original = adapter.client
        adapter.client = httpx.AsyncClient(
            base_url=original.base_url,
            headers=original.headers,
            cookies=original.cookies,
            follow_redirects=False,
            transport=httpx.MockTransport(handler),
        )
        await original.aclose()
        adapters.append(adapter)
        return adapter

    yield create
    for adapter in adapters:
        await adapter.close()


@pytest_asyncio.fixture
async def browsing_adapter_factory():
    adapters = []

    async def create(handler, credentials=True):
        adapter = BrowsingAdapter(
            {"sessdata": "secret-session", "csrf": "secret-csrf"} if credentials else {}
        )
        original = adapter.client
        adapter.client = httpx.AsyncClient(
            base_url=original.base_url,
            headers=original.headers,
            cookies=original.cookies,
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        await original.aclose()
        adapter._read_spacing = 0
        adapters.append(adapter)
        return adapter

    yield create
    for adapter in adapters:
        await adapter.close()
