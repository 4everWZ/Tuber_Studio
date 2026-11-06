"""Bounded public-CDN download with pinned DNS and format inspection elsewhere."""

import asyncio
import hashlib
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

CDNS = ("kuwo.cn", "migu.cn", "cmvideo.cn", "miguvideo.com")


async def public_address(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 80, 443}
        or not any(host == domain or host.endswith("." + domain) for domain in CDNS)
    ):
        raise ValueError("音源下载地址不在已支持的公开音乐 CDN 范围")
    addresses = await asyncio.get_running_loop().getaddrinfo(
        host,
        parsed.port or (443 if parsed.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("音源下载地址解析到非公网地址")
    addresses.sort(key=lambda row: row[0] != socket.AF_INET)
    return parsed, addresses[0][4][0]


async def download(url, path, maximum):
    digest, size = hashlib.sha256(), 0
    async with (
        asyncio.timeout(90),
        httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=8),
            trust_env=False,
            follow_redirects=False,
        ) as client,
    ):
        for _ in range(6):
            parsed, address = await public_address(url)
            # Pin the checked address; preserve Host and TLS hostname verification.
            target = httpx.URL(url).copy_with(host=address)
            async with client.stream(
                "GET",
                target,
                headers={
                    "Host": parsed.hostname,
                    "Accept-Encoding": "identity",
                    "User-Agent": "Dart/2.19 (dart:io)",
                },
                extensions={"sni_hostname": parsed.hostname},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = str(httpx.URL(url).join(response.headers.get("location", "")))
                    continue
                response.raise_for_status()
                expected = response.headers.get("content-length")
                if expected and int(expected) > maximum:
                    raise ValueError("歌曲文件超过下载／缓存大小上限")
                with path.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > maximum:
                            raise ValueError("歌曲文件超过下载／缓存大小上限")
                        digest.update(chunk)
                        output.write(chunk)
                if expected and size != int(expected):
                    raise ValueError("歌曲下载不完整")
                if size < 128:
                    raise ValueError("音源未返回有效音频")
                return digest.hexdigest(), size
        raise ValueError("音源下载重定向次数过多")
