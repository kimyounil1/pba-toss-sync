"""X timeline via Playwright + logged-in session (includes subscriber-only posts)."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator

from src.config import AppConfig
from src.db import StateDB
from src.x_types import Tweet

logger = logging.getLogger(__name__)

SINCE_ID_KEY = "since_id"
PROFILE_URL = "https://x.com/{username}"
SUPERFOLLOWS_URL = "https://x.com/{username}/superfollows"
STATUS_URL = "https://x.com/i/status/{tweet_id}"


def parse_tweet_id(value: str) -> str:
    """Extract numeric tweet id from a status URL or raw id."""
    value = value.strip()
    match = re.search(r"/status/(\d+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"\d+", value):
        return value
    raise ValueError(f"Not a tweet id or status URL: {value}")


class XBrowserMonitor:
    def __init__(self, config: AppConfig, db: StateDB | None = None) -> None:
        self.config = config
        self.db = db
        self.session_path = Path(config.x_session_file)

    def session_ready(self) -> bool:
        return self.session_path.is_file()

    async def poll_loop(self) -> AsyncIterator[Tweet]:
        if not self.session_ready():
            logger.error(
                "X session missing at %s — run: bash scripts/x_auth_login.sh",
                self.session_path,
            )
            while True:
                await asyncio.sleep(3600)
            return

        while True:
            try:
                tweets = await self.fetch_merged_tweets(
                    days=2, max_scrolls=min(self.config.x_scroll_rounds, 8)
                )
                since_id = self.db.get_cursor(SINCE_ID_KEY) if self.db else None
                since_int = int(since_id) if since_id else 0
                new_tweets = [t for t in tweets if int(t.id) > since_int]
                new_tweets.sort(key=lambda t: int(t.id))
                if new_tweets and self.db:
                    self.db.set_cursor(SINCE_ID_KEY, new_tweets[-1].id)
                for tweet in new_tweets:
                    if not self.db or not self.db.tweet_seen(tweet.id):
                        yield tweet
            except Exception as exc:
                logger.exception("X browser poll failed: %s", exc)
            await asyncio.sleep(self.config.poll_interval_sec)

    async def fetch_tweets_historical(
        self, *, days: int = 30, supplement_tweet_ids: list[str] | None = None
    ) -> list[Tweet]:
        tweets = await self.fetch_merged_tweets(
            days=days, max_scrolls=self.config.x_scroll_rounds
        )
        extra_ids = list(supplement_tweet_ids or [])
        gaps = find_timeline_gaps(tweets, min_gap_minutes=90)
        if gaps:
            logger.warning(
                "X profile has %d timeline gap(s) — supplement with --tweet-url (subscriber posts need direct URL)",
                len(gaps),
            )
        if extra_ids:
            by_id = {t.id: t for t in tweets}
            need = [parse_tweet_id(raw) for raw in extra_ids if parse_tweet_id(raw) not in by_id]
            if need:
                for tweet in await self.fetch_tweets_by_ids(need):
                    by_id.setdefault(tweet.id, tweet)
                tweets = sorted(by_id.values(), key=lambda t: t.created_at or "")
        logger.info(
            "X browser: %d tweets (%d days), newest=%s",
            len(tweets),
            days,
            tweets[-1].created_at if tweets else "n/a",
        )
        return tweets

    async def fetch_recent_for_backfill(self, limit: int = 20) -> list[Tweet]:
        tweets = await self.fetch_merged_tweets(days=14, max_scrolls=self.config.x_scroll_rounds)
        return tweets[-limit:]

    async def fetch_tweets_by_ids(self, tweet_ids: list[str]) -> list[Tweet]:
        """Fetch multiple status pages in one browser session."""
        if not tweet_ids:
            return []
        from playwright.async_api import async_playwright

        if not self.session_ready():
            raise FileNotFoundError(
                f"X session not found: {self.session_path}. Run scripts/x_auth_login.sh"
            )

        ids = [parse_tweet_id(t) for t in tweet_ids]
        found: dict[str, Tweet] = {}
        async with async_playwright() as playwright:
            launch_kwargs = {
                "headless": self.config.x_headless,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            try:
                browser = await playwright.chromium.launch(channel="chrome", **launch_kwargs)
            except Exception:
                browser = await playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                storage_state=str(self.session_path),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = await context.new_page()
            try:
                for tid in ids:
                    if tid in found:
                        continue
                    url = STATUS_URL.format(tweet_id=tid)
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                        await page.wait_for_selector(
                            'article[data-testid="tweet"]', timeout=20_000
                        )
                        batch = await self._extract_visible_tweets(page)
                        for tweet in batch:
                            if tweet.id == tid:
                                found[tid] = tweet
                                break
                        if tid not in found and batch:
                            found[tid] = batch[0]
                    except Exception as exc:
                        logger.debug("Skip probe id %s: %s", tid, exc)
            finally:
                await context.close()
                await browser.close()
        return list(found.values())

    async def fetch_tweet_by_id(self, tweet_id: str) -> Tweet:
        """Fetch one post by status URL (works for subscriber-only posts missing from profile scroll)."""
        tweet_id = parse_tweet_id(tweet_id)
        from playwright.async_api import async_playwright

        if not self.session_ready():
            raise FileNotFoundError(
                f"X session not found: {self.session_path}. Run scripts/x_auth_login.sh"
            )

        url = STATUS_URL.format(tweet_id=tweet_id)
        async with async_playwright() as playwright:
            launch_kwargs = {
                "headless": self.config.x_headless,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            try:
                browser = await playwright.chromium.launch(channel="chrome", **launch_kwargs)
            except Exception:
                browser = await playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                storage_state=str(self.session_path),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_selector('article[data-testid="tweet"]', timeout=45_000)
                batch = await self._extract_visible_tweets(page)
                for tweet in batch:
                    if tweet.id == tweet_id:
                        return tweet
                if batch:
                    return batch[0]
                raise RuntimeError(f"Tweet {tweet_id} not found at {url}")
            finally:
                await context.close()
                await browser.close()

    def _timeline_urls(self) -> list[tuple[str, str]]:
        user = self.config.pba_username.lstrip("@")
        urls = [(PROFILE_URL.format(username=user), "profile")]
        if self.config.x_include_superfollows:
            urls.append((SUPERFOLLOWS_URL.format(username=user), "superfollows"))
        return urls

    async def fetch_merged_tweets(self, *, days: int, max_scrolls: int) -> list[Tweet]:
        """Profile + /superfollows (subscription posts) in one browser session."""
        from playwright.async_api import async_playwright

        if not self.session_ready():
            raise FileNotFoundError(
                f"X session not found: {self.session_path}. Run scripts/x_auth_login.sh"
            )

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        async with async_playwright() as playwright:
            launch_kwargs = {
                "headless": self.config.x_headless,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            try:
                browser = await playwright.chromium.launch(channel="chrome", **launch_kwargs)
            except Exception:
                logger.warning("Chrome channel unavailable; using bundled Chromium")
                browser = await playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                storage_state=str(self.session_path),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = await context.new_page()
            try:
                by_id: dict[str, Tweet] = {}
                for timeline_url, label in self._timeline_urls():
                    await self._scrape_timeline(
                        page,
                        url=timeline_url,
                        label=label,
                        cutoff=cutoff,
                        by_id=by_id,
                        max_scrolls=max_scrolls,
                    )
                    logger.info("X %s collected; total unique tweets: %d", label, len(by_id))

                tweets = sorted(by_id.values(), key=lambda t: t.created_at or "")
                gaps = find_timeline_gaps(tweets)
                if gaps:
                    logger.warning(
                        "Merged timelines still have %d gap(s); use --tweet-url if needed",
                        len(gaps),
                    )
                return tweets
            finally:
                await context.close()
                await browser.close()

    async def _scrape_timeline(
        self,
        page,
        *,
        url: str,
        label: str,
        cutoff: datetime,
        by_id: dict[str, Tweet],
        max_scrolls: int,
    ) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_selector('article[data-testid="tweet"]', timeout=45_000)

        stale_rounds = 0
        stale_limit = 6 if max_scrolls >= 15 else 3
        extra_passes = 0

        async def _scroll_page() -> None:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(self.config.x_scroll_delay_sec)

        total_rounds = max_scrolls
        round_idx = 0
        while round_idx < total_rounds:
            batch = await self._extract_visible_tweets(page)
            added = 0
            for tweet in batch:
                if tweet.id in by_id:
                    continue
                created = _parse_created_at(tweet.created_at)
                if created and created < cutoff:
                    continue
                if not self.config.include_replies and tweet.is_reply:
                    continue
                by_id[tweet.id] = tweet
                added += 1

            if added == 0:
                stale_rounds += 1
            else:
                stale_rounds = 0
            if stale_rounds >= stale_limit:
                break

            in_timeline = [t for t in by_id.values() if _parse_created_at(t.created_at)]
            oldest = min(
                in_timeline,
                key=lambda t: _parse_created_at(t.created_at) or datetime.max.replace(tzinfo=timezone.utc),
                default=None,
            )
            if oldest and (_parse_created_at(oldest.created_at) or datetime.max.replace(tzinfo=timezone.utc)) < cutoff:
                break

            await _scroll_page()
            round_idx += 1

        gaps = find_timeline_gaps(sorted(by_id.values(), key=lambda t: t.created_at or ""))
        while gaps and extra_passes < 8 and round_idx < total_rounds + 12:
            logger.warning(
                "X %s gap ~%s min (%s → %s) extra scroll %d/8",
                label,
                gaps[0]["gap_minutes"],
                gaps[0]["after_at"],
                gaps[0]["before_at"],
                extra_passes + 1,
            )
            await _scroll_page()
            for tweet in await self._extract_visible_tweets(page):
                created = _parse_created_at(tweet.created_at)
                if created and created < cutoff:
                    continue
                if not self.config.include_replies and tweet.is_reply:
                    continue
                by_id.setdefault(tweet.id, tweet)
            gaps = find_timeline_gaps(sorted(by_id.values(), key=lambda t: t.created_at or ""))
            extra_passes += 1
            round_idx += 1

    async def fetch_profile_tweets(self, *, days: int, max_scrolls: int) -> list[Tweet]:
        """Backward-compatible alias for profile-only callers."""
        return await self.fetch_merged_tweets(days=days, max_scrolls=max_scrolls)

    async def _extract_visible_tweets(self, page) -> list[Tweet]:
        raw: list[dict] = await page.evaluate(
            """() => {
            const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
            return articles.map((article) => {
                const statusLink = article.querySelector('a[href*="/status/"]');
                let id = '';
                if (statusLink) {
                    const m = statusLink.getAttribute('href').match(/\\/status\\/(\\d+)/);
                    if (m) id = m[1];
                }
                const timeEl = article.querySelector('time');
                const created_at = timeEl ? (timeEl.getAttribute('datetime') || '') : '';
                const textEl = article.querySelector('[data-testid="tweetText"]');
                let text = textEl ? textEl.innerText : '';
                if (!text) text = article.innerText.slice(0, 500);
                const social = article.querySelector('[data-testid="socialContext"]');
                const is_reply = !!(social && /replying/i.test(social.innerText));
                return { id, text, created_at, is_reply };
            }).filter((t) => t.id);
        }"""
        )
        return [
            Tweet(
                id=str(item["id"]),
                text=str(item.get("text", "")),
                created_at=str(item.get("created_at", "")),
                is_reply=bool(item.get("is_reply")),
            )
            for item in raw
        ]


def _parse_created_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


TWITTER_EPOCH_MS = 1288834974657


def snowflake_to_ms(tweet_id: int) -> int:
    return (tweet_id >> 22) + TWITTER_EPOCH_MS


def ms_to_snowflake(ms: int) -> int:
    return (ms - TWITTER_EPOCH_MS) << 22


def gap_probe_ids(left_id: int, right_id: int, *, steps: int = 5) -> list[str]:
    """Candidate tweet ids between two snowflakes (for gap fill attempts)."""
    start_ms = snowflake_to_ms(left_id)
    end_ms = snowflake_to_ms(right_id)
    if end_ms <= start_ms:
        return []
    out: list[str] = []
    for i in range(1, steps + 1):
        ms = start_ms + (end_ms - start_ms) * i // (steps + 1)
        out.append(str(ms_to_snowflake(ms)))
    return out


def find_timeline_gaps(
    tweets: list[Tweet], *, min_gap_minutes: int = 45
) -> list[dict[str, str]]:
    """Flag likely missing posts when consecutive timeline entries are far apart."""
    gaps: list[dict[str, str]] = []
    parsed = [
        (t, _parse_created_at(t.created_at))
        for t in sorted(tweets, key=lambda x: x.created_at or "")
    ]
    parsed = [(t, dt) for t, dt in parsed if dt is not None]
    for (left, left_dt), (right, right_dt) in zip(parsed, parsed[1:]):
        delta = right_dt - left_dt
        if delta.total_seconds() >= min_gap_minutes * 60:
            gaps.append(
                {
                    "after_id": left.id,
                    "after_at": left.created_at,
                    "before_id": right.id,
                    "before_at": right.created_at,
                    "gap_minutes": str(int(delta.total_seconds() // 60)),
                }
            )
    return gaps
