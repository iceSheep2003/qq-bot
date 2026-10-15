"""NapCat byte acquisition with pinned public DNS and explicit local mounts.

No arbitrary downloader tool is exposed. HTTPS is limited to QQ image/media
hosts; redirects, proxies, private IPs and paths outside owner mounts are denied.
NapCat handles record conversion through get_record(out_format=wav).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import json
import os
import logging
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from qqbot.contracts.schema import ContractError

MAX_BYTES = 8 * 1024 * 1024


class _MediaRequestFilter(logging.Filter):
    """HTTPX request logs must not retain QQ's signed URL query parameters."""

    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(
                arg.copy_with(query=None, fragment=None)
                if isinstance(arg, httpx.URL)
                else arg
                for arg in record.args
            )
        return True


_request_filter = _MediaRequestFilter()


def allowed_host(host):
    return (
        host == "qpic.cn" or host.endswith(".qpic.cn") or host == "multimedia.nt.qq.com"
    )


class NapCatMediaSource:
    def __init__(self, gateway, *, roots=(), client=None):
        self.gateway = gateway
        logger = logging.getLogger("httpx")
        if _request_filter not in logger.filters:
            logger.addFilter(_request_filter)
        self.roots = tuple(Path(root).resolve(strict=True) for root in roots)
        if any(
            not root.is_dir() or root in {Path("/"), Path.home()} for root in self.roots
        ):
            raise ValueError("Media roots must be explicit narrow directories")
        self.client = client or httpx.AsyncClient(
            timeout=8, follow_redirects=False, trust_env=False
        )

    async def close(self):
        await self.client.aclose()

    async def acquire(self, part):
        try:
            return await self._acquire(part)
        except httpx.HTTPError:
            raise ConnectionError("Platform media transport failed") from None

    async def _acquire(self, part):
        if part.kind == "image" and part.url:
            return await self._download(part.url)
        if not part.file_ref or len(part.file_ref) > 2000:
            raise ContractError("No platform media locator")
        try:
            data = await self.gateway.call(
                "get_image" if part.kind == "image" else "get_record",
                {
                    "file": part.file_ref,
                    **({"out_format": "wav"} if part.kind == "audio" else {}),
                },
            )
        except Exception:
            raise ConnectionError("Platform media unavailable") from None
        if not isinstance(data, dict):
            raise ContractError("Invalid platform media response")
        if data.get("url"):
            return await self._download(str(data["url"]))
        path = Path(str(data.get("file") or ""))
        if not path.is_absolute():
            raise ContractError("Unmapped platform media path")
        return await asyncio.to_thread(self._read_local, path)

    def _read_local(self, path):
        path = path.resolve(strict=True)
        if (
            not any(path.is_relative_to(root) for root in self.roots)
            or not path.is_file()
        ):
            raise ContractError("Media path is not in an owner mount")
        with path.open("rb") as stream:
            payload = stream.read(MAX_BYTES + 1)
        if not 0 < len(payload) <= MAX_BYTES:
            raise ContractError("Media byte budget exceeded")
        return payload

    async def _download(self, url):
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not allowed_host(host)
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.port not in {None, 443}
        ):
            raise ContractError("Unapproved platform media URL")
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host, 443, type=socket.SOCK_STREAM
        )
        ips = sorted({address[4][0] for address in addresses})
        if not ips or any(
            not ipaddress.ip_address(address).is_global for address in ips
        ):
            raise ContractError("Non-public media address")
        # Pin the validated address for the connection, retaining the original
        # TLS SNI and Host. There is no second hostname resolution at transport.
        pinned = httpx.URL(url).copy_with(host=ips[0])
        chunks, size = [], 0
        async with self.client.stream(
            "GET",
            pinned,
            headers={"Host": host, "Accept-Encoding": "identity"},
            extensions={"sni_hostname": host},
        ) as response:
            if response.status_code != 200:
                raise ConnectionError("Platform media fetch failed")
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ContractError("Compressed media transport is not allowed")
            declared = response.headers.get("content-length")
            if declared and (not declared.isdigit() or int(declared) > MAX_BYTES):
                raise ContractError("Media byte budget exceeded")
            async for chunk in response.aiter_bytes(chunk_size=65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ContractError("Media byte budget exceeded")
                chunks.append(chunk)
        if not size:
            raise ContractError("Empty platform media")
        return b"".join(chunks)


def configured_media_source(gateway):
    roots = json.loads(os.getenv("BOT_NAPCAT_MEDIA_ROOTS", "[]"))
    if (
        not isinstance(roots, list)
        or len(roots) > 4
        or any(
            not isinstance(root, str) or not Path(root).is_absolute() for root in roots
        )
    ):
        raise ValueError(
            "BOT_NAPCAT_MEDIA_ROOTS must be a JSON list of absolute mount paths"
        )
    return NapCatMediaSource(gateway, roots=roots)
