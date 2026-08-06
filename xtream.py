"""Thin async client for the Xtream Codes player_api."""
import httpx

import config

# The provider is slow on the big list endpoints, so the read timeout is
# generous while the connect timeout stays short enough to fail fast.
TIMEOUT = httpx.Timeout(connect=15.0, read=240.0, write=30.0, pool=30.0)


class Xtream:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, action: str | None = None, **params):
        query = {"username": config.USER, "password": config.PASS}
        if action:
            query["action"] = action
        query.update({k: str(v) for k, v in params.items()})
        r = await self._client.get(f"{config.HOST}/player_api.php", params=query)
        r.raise_for_status()
        return r.json()

    async def account(self) -> dict:
        return await self._call()

    async def live_categories(self):
        return await self._call("get_live_categories")

    async def vod_categories(self):
        return await self._call("get_vod_categories")

    async def series_categories(self):
        return await self._call("get_series_categories")

    async def live_streams(self):
        return await self._call("get_live_streams")

    async def vod_streams(self):
        return await self._call("get_vod_streams")

    async def series(self):
        return await self._call("get_series")

    async def series_info(self, series_id: int) -> dict:
        return await self._call("get_series_info", series_id=series_id)
