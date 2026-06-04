"""X (Twitter) official API monitor — public tweets only (no subscriber posts)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import httpx

from src.config import AppConfig
from src.db import StateDB
from src.x_types import Tweet

logger = logging.getLogger(__name__)

X_API_BASE = "https://api.x.com/2"
USER_ID_KEY = "pba_user_id"
SINCE_ID_KEY = "since_id"


class XApiMonitor:
    def __init__(self, config: AppConfig, db: StateDB | None = None) -> None:
        self.config = config
        self.db = db
        self._headers = {"Authorization": f"Bearer {config.x_bearer_token}"}
        self._user_id_cache: str | None = None

    async def resolve_user_id(self, client: httpx.AsyncClient) -> str:
        if self.db:
            cached = self.db.get_cursor(USER_ID_KEY)
            if cached:
                return cached
        elif self._user_id_cache:
            return self._user_id_cache

        url = f"{X_API_BASE}/users/by/username/{self.config.pba_username}"
        resp = await client.get(url, headers=self._headers)
        resp.raise_for_status()
        user_id = str(resp.json()["data"]["id"])
        if self.db:
            self.db.set_cursor(USER_ID_KEY, user_id)
        else:
            self._user_id_cache = user_id
        return user_id

    async def fetch_new_tweets(self, client: httpx.AsyncClient) -> list[Tweet]:
        user_id = await self.resolve_user_id(client)
        since_id = self.db.get_cursor(SINCE_ID_KEY) if self.db else None
        params: dict[str, str | int] = {
            "max_results": 10,
            "tweet.fields": "created_at,text,author_id",
            "exclude": self._exclude_param(),
        }
        if since_id:
            params["since_id"] = since_id

        url = f"{X_API_BASE}/users/{user_id}/tweets"
        resp = await client.get(url, headers=self._headers, params=params)
        resp.raise_for_status()
        payload = resp.json()
        meta = payload.get("meta") or {}
        if self.db and meta.get("newest_id"):
            self.db.set_cursor(SINCE_ID_KEY, str(meta["newest_id"]))

        tweets: list[Tweet] = []
        for item in reversed(payload.get("data") or []):
            tweet = self._tweet_from_item(item)
            if not self.db or not self.db.tweet_seen(tweet.id):
                tweets.append(tweet)
        return tweets

    async def poll_loop(self) -> AsyncIterator[Tweet]:
        if not self.config.x_bearer_token:
            logger.warning("X_BEARER_TOKEN not set; X API monitor idle")
            while True:
                await asyncio.sleep(3600)
            return

        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                try:
                    for tweet in await self.fetch_new_tweets(client):
                        yield tweet
                except httpx.HTTPError as exc:
                    logger.exception("X API poll failed: %s", exc)
                await asyncio.sleep(self.config.poll_interval_sec)

    def _exclude_param(self) -> str:
        return "retweets" if self.config.include_replies else "replies,retweets"

    def _tweet_from_item(self, item: dict) -> Tweet:
        return Tweet(
            id=str(item["id"]),
            text=item.get("text", ""),
            created_at=item.get("created_at", ""),
            author_id=item.get("author_id"),
        )

    async def _fetch_timeline_pages(
        self,
        client: httpx.AsyncClient,
        user_id: str,
        *,
        start_time: str | None = None,
        max_pages: int = 50,
    ) -> list[Tweet]:
        tweets: list[Tweet] = []
        pagination_token: str | None = None
        for _ in range(max_pages):
            params: dict[str, str | int] = {
                "max_results": 100,
                "tweet.fields": "created_at,text,author_id",
                "exclude": self._exclude_param(),
            }
            if start_time:
                params["start_time"] = start_time
            if pagination_token:
                params["pagination_token"] = pagination_token

            resp = await client.get(
                f"{X_API_BASE}/users/{user_id}/tweets",
                headers=self._headers,
                params=params,
            )
            resp.raise_for_status()
            payload = resp.json()
            for item in payload.get("data") or []:
                tweets.append(self._tweet_from_item(item))
            pagination_token = (payload.get("meta") or {}).get("next_token")
            if not pagination_token or not payload.get("data"):
                break
        return tweets

    async def fetch_tweets_historical(
        self,
        client: httpx.AsyncClient,
        *,
        days: int = 30,
        max_pages: int = 50,
    ) -> list[Tweet]:
        user_id = await self.resolve_user_id(client)
        start_time = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        head = await self._fetch_timeline_pages(client, user_id, start_time=None, max_pages=1)
        body = await self._fetch_timeline_pages(
            client, user_id, start_time=start_time, max_pages=max_pages
        )
        by_id = {t.id: t for t in head + body}
        tweets = sorted(by_id.values(), key=lambda t: t.created_at or "")
        logger.info(
            "X API: %d tweets (%d days), newest=%s",
            len(tweets),
            days,
            tweets[-1].created_at if tweets else "n/a",
        )
        return tweets

    async def fetch_recent_for_backfill(self, limit: int = 20) -> list[Tweet]:
        if not self.config.x_bearer_token:
            return []
        async with httpx.AsyncClient(timeout=30.0) as client:
            if limit > 100:
                return await self.fetch_tweets_historical(client, days=30)
            user_id = await self.resolve_user_id(client)
            resp = await client.get(
                f"{X_API_BASE}/users/{user_id}/tweets",
                headers=self._headers,
                params={
                    "max_results": min(limit, 100),
                    "tweet.fields": "created_at,text,author_id",
                    "exclude": self._exclude_param(),
                },
            )
            resp.raise_for_status()
            return [self._tweet_from_item(item) for item in resp.json().get("data") or []]
