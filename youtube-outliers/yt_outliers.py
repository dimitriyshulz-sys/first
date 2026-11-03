#!/usr/bin/env python3
"""
yt_outliers — поиск роликов YouTube с аномальными просмотрами.

Идея: ролик считается «аномальным» (outlier), если он набрал во много раз больше
просмотров, чем обычно набирает его канал (медиана последних роликов того же
формата), и/или много просмотров относительно числа подписчиков.

Два источника данных:
  * ytdlp — без ключа, через yt-dlp (парсит публичные страницы YouTube);
  * api   — официальный YouTube Data API v3 (нужен ключ, данные точнее).

Примеры:
  python yt_outliers.py -q "рецепты пп" --period month --min-score 5
  python yt_outliers.py -q "minecraft" --type shorts --max-subs 50000
  python yt_outliers.py -c @PrestonPlayz -c UCxxxx --min-score 2
  python yt_outliers.py --config criteria.example.json
"""
from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHORTS_MAX_SECONDS = 180  # с октября 2024 Shorts могут длиться до 3 минут

PERIODS = {  # период -> (код фильтра YouTube, дней)
    "hour": (1, 1 / 24),
    "today": (2, 1),
    "week": (3, 7),
    "month": (4, 31),
    "year": (5, 366),
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Модель данных
# --------------------------------------------------------------------------- #
@dataclass
class Video:
    id: str
    title: str = ""
    channel_id: str = ""
    channel: str = ""
    views: int | None = None
    published: datetime | None = None
    duration: int | None = None  # секунды
    is_short: bool | None = None
    likes: int | None = None
    comments: int | None = None
    description: str = ""

    @property
    def url(self) -> str:
        if self.is_short:
            return f"https://www.youtube.com/shorts/{self.id}"
        return f"https://www.youtube.com/watch?v={self.id}"


@dataclass
class Channel:
    id: str
    title: str = ""
    subscribers: int | None = None
    videos: list[Video] = field(default_factory=list)  # последние ролики (база)


@dataclass
class Result:
    video: Video
    subscribers: int | None
    baseline: float | None  # медиана просмотров канала
    baseline_n: int
    score: float | None  # views / baseline
    views_per_sub: float | None
    age_days: float | None
    views_per_day: float | None
    zscore: float | None  # робастный z-score по log10(просмотров)
    ai_score: int = 0  # 0–100: эвристическая вероятность, что ролик сделан с помощью ИИ
    ai_reasons: str = ""

    def row(self) -> dict:
        v = self.video
        return {
            "score": _r(self.score, 2),
            "views": v.views,
            "baseline_median": _r(self.baseline, 0),
            "baseline_n": self.baseline_n,
            "subscribers": self.subscribers,
            "views_per_sub": _r(self.views_per_sub, 2),
            "views_per_day": _r(self.views_per_day, 0),
            "zscore": _r(self.zscore, 2),
            "ai_score": self.ai_score,
            "ai_reasons": self.ai_reasons,
            "age_days": _r(self.age_days, 1),
            "published": v.published.date().isoformat() if v.published else None,
            "type": "shorts" if v.is_short else "long",
            "duration_sec": v.duration,
            "likes": v.likes,
            "comments": v.comments,
            "title": v.title,
            "channel": v.channel,
            "channel_id": v.channel_id,
            "video_id": v.id,
            "url": v.url,
        }


def _r(x, nd):
    if x is None:
        return None
    return round(x, nd) if nd else int(round(x))


# --------------------------------------------------------------------------- #
# Кэш каналов (экономит время и квоту API)
# --------------------------------------------------------------------------- #
class Cache:
    def __init__(self, path: Path, ttl_hours: float):
        self.path, self.ttl = path, ttl_hours * 3600
        self.data: dict = {}
        if self.ttl > 0 and path.exists():
            try:
                self.data = json.loads(path.read_text("utf-8"))
            except Exception:
                self.data = {}

    def get(self, key: str):
        item = self.data.get(key)
        if item and time.time() - item["t"] < self.ttl:
            return item["v"]
        return None

    def set(self, key: str, value) -> None:
        if self.ttl > 0:
            self.data[key] = {"t": time.time(), "v": value}

    def save(self) -> None:
        if self.ttl > 0:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False), "utf-8")


def channel_to_json(ch: Channel) -> dict:
    d = asdict(ch)
    for v in d["videos"]:
        v["published"] = v["published"].isoformat() if v["published"] else None
    return d


def channel_from_json(d: dict) -> Channel:
    vids = []
    for v in d.get("videos", []):
        v = dict(v)
        v["published"] = datetime.fromisoformat(v["published"]) if v.get("published") else None
        vids.append(Video(**v))
    return Channel(id=d["id"], title=d.get("title", ""), subscribers=d.get("subscribers"), videos=vids)


# --------------------------------------------------------------------------- #
# Источник: yt-dlp (без ключа)
# --------------------------------------------------------------------------- #
class _QuietLogger:
    """Глушит штатные ошибки yt-dlp вроде «у канала нет вкладки Shorts»."""

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass

    def error(self, msg):
        if "does not have a" in msg or "not a bot" in msg:
            return
        log("  ! " + re.sub(r"^ERROR:\s*", "", msg))


class YtDlpBackend:
    name = "ytdlp"

    def __init__(self, cookies: str | None = None, cookies_from_browser: str | None = None):
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            sys.exit("Не установлен yt-dlp: pip install -U yt-dlp  (или укажите --api-key)")
        self.base_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "noprogress": True,
            "ignoreerrors": True,
            "extractor_args": {"youtubetab": {"approximate_date": [""]}},
            "logger": _QuietLogger(),
        }
        if cookies:
            self.base_opts["cookiefile"] = cookies
        if cookies_from_browser:
            self.base_opts["cookiesfrombrowser"] = (cookies_from_browser,)

    def _extract(self, url: str, limit: int | None = None, flat: bool = True):
        import yt_dlp

        opts = dict(self.base_opts)
        if limit:
            opts["playlistend"] = limit
        if not flat:
            opts.pop("extract_flat")
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    @staticmethod
    def _search_sp(period: str | None, duration: str | None, sort: str) -> str:
        """Собирает параметр sp (protobuf) для фильтров поиска YouTube."""
        filters = b""
        if period:
            filters += bytes([0x08, PERIODS[period][0]])
        filters += bytes([0x10, 1])  # только видео
        dur_code = {"short": 1, "long": 2, "medium": 3}.get(duration or "")
        if dur_code:
            filters += bytes([0x18, dur_code])
        msg = b""
        sort_code = {"relevance": 0, "date": 2, "views": 3, "rating": 1}[sort]
        if sort_code:
            msg += bytes([0x08, sort_code])
        msg += bytes([0x12, len(filters)]) + filters
        return base64.urlsafe_b64encode(msg).decode()

    def search(self, query: str, limit: int, period: str | None, duration: str | None,
               sort: str, **_) -> list[Video]:
        sp = self._search_sp(period, duration, sort)
        url = ("https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
               + "&sp=" + urllib.parse.quote(sp))
        info = self._extract(url, limit=limit) or {}
        out = []
        for e in info.get("entries") or []:
            if not e or not e.get("id") or not e.get("channel_id"):
                continue
            v = self._video_from_flat(e)
            if v.views:  # 0/None — обычно трансляции и премьеры
                out.append(v)
        return out

    @staticmethod
    def _video_from_flat(e: dict, is_short: bool | None = None) -> Video:
        ts = e.get("timestamp") or e.get("release_timestamp")
        published = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
        if not published and e.get("upload_date"):
            published = datetime.strptime(e["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc)
        dur = e.get("duration")
        if is_short is None:
            if "/shorts/" in (e.get("url") or ""):
                is_short = True
            elif dur:
                is_short = dur <= SHORTS_MAX_SECONDS
        return Video(
            id=e["id"], title=e.get("title") or "", channel_id=e.get("channel_id") or "",
            channel=e.get("channel") or e.get("uploader") or "",
            views=e.get("view_count"), published=published,
            duration=int(dur) if dur else None, is_short=is_short,
            likes=e.get("like_count"), comments=e.get("comment_count"),
            description=(e.get("description") or "")[:500],
        )

    @staticmethod
    def _channel_base_url(ref: str) -> str:
        ref = ref.strip()
        if ref.startswith("http"):
            return re.sub(r"/(videos|shorts|streams|featured|playlists|community)/?$", "", ref.rstrip("/"))
        if ref.startswith("UC") and len(ref) == 24:
            return f"https://www.youtube.com/channel/{ref}"
        return "https://www.youtube.com/@" + ref.lstrip("@")

    def channel(self, ref: str, limit: int, kinds: set[str]) -> Channel | None:
        base = self._channel_base_url(ref)
        ch = None
        for kind in ("long", "shorts"):
            if kind not in kinds:
                continue
            tab = "videos" if kind == "long" else "shorts"
            info = self._extract(f"{base}/{tab}", limit=limit)
            if not info:
                continue
            if ch is None:
                ch = Channel(id=info.get("channel_id") or ref,
                             title=info.get("channel") or info.get("uploader") or "",
                             subscribers=info.get("channel_follower_count"))
            for e in info.get("entries") or []:
                if e and e.get("id"):
                    v = self._video_from_flat(e, is_short=(kind == "shorts"))
                    v.channel_id, v.channel = ch.id, ch.title
                    ch.videos.append(v)
        return ch

    def enrich(self, videos: list[Video]) -> None:
        """Точные дата/лайки/комментарии — по одному запросу на ролик (медленно)."""
        fails = 0
        for v in videos:
            if fails >= 3:
                log("  ! YouTube блокирует подробные запросы (нужны --cookies). Пропускаю уточнение.")
                return
            info = self._extract(f"https://www.youtube.com/watch?v={v.id}", flat=False)
            if not info:
                fails += 1
                continue
            fails = 0
            if info.get("upload_date"):
                v.published = datetime.strptime(info["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc)
            v.views = info.get("view_count") or v.views
            v.likes, v.comments = info.get("like_count"), info.get("comment_count")


# --------------------------------------------------------------------------- #
# Источник: YouTube Data API v3 (нужен ключ)
# --------------------------------------------------------------------------- #
class ApiBackend:
    name = "api"
    URL = "https://www.googleapis.com/youtube/v3/"

    def __init__(self, key: str):
        self.key = key
        self.quota_used = 0

    def _get(self, method: str, cost: int, **params) -> dict:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params["key"] = self.key
        url = self.URL + method + "?" + urllib.parse.urlencode(params)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    self.quota_used += cost
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code in (500, 503) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                try:
                    reason = json.loads(body)["error"]["message"]
                except Exception:
                    reason = body[:300]
                if "quota" in reason.lower():
                    sys.exit(f"Квота YouTube API исчерпана: {reason}")
                raise RuntimeError(f"YouTube API {method}: {e.code} {reason}") from None
        return {}

    @staticmethod
    def _iso_duration(s: str | None) -> int | None:
        m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
        if not m:
            return None
        d, h, mi, se = (int(x or 0) for x in m.groups())
        return d * 86400 + h * 3600 + mi * 60 + se

    def videos(self, ids: list[str]) -> list[Video]:
        out = []
        for i in range(0, len(ids), 50):
            data = self._get("videos", 1, part="snippet,statistics,contentDetails",
                             id=",".join(ids[i:i + 50]), maxResults=50)
            for it in data.get("items", []):
                sn, st = it["snippet"], it.get("statistics", {})
                if sn.get("liveBroadcastContent") in ("live", "upcoming"):
                    continue
                dur = self._iso_duration(it.get("contentDetails", {}).get("duration"))
                out.append(Video(
                    id=it["id"], title=sn.get("title", ""), channel_id=sn.get("channelId", ""),
                    channel=sn.get("channelTitle", ""),
                    views=int(st["viewCount"]) if "viewCount" in st else None,
                    published=datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00")),
                    duration=dur, is_short=(dur is not None and dur <= SHORTS_MAX_SECONDS),
                    likes=int(st["likeCount"]) if "likeCount" in st else None,
                    comments=int(st["commentCount"]) if "commentCount" in st else None,
                ))
        return out

    def search(self, query: str, limit: int, period: str | None, duration: str | None,
               sort: str, days: float | None = None, region: str | None = None,
               lang: str | None = None) -> list[Video]:
        span = days or (PERIODS[period][1] if period else None)
        after = None
        if span:
            after = (datetime.now(timezone.utc) - timedelta(days=span)).strftime("%Y-%m-%dT%H:%M:%SZ")
        order = {"relevance": "relevance", "date": "date", "views": "viewCount", "rating": "rating"}[sort]
        ids, token = [], None
        while len(ids) < limit:
            data = self._get("search", 100, part="id", type="video", q=query,
                             maxResults=min(50, limit - len(ids)), order=order,
                             publishedAfter=after, videoDuration=duration or None,
                             regionCode=region, relevanceLanguage=lang, pageToken=token)
            ids += [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]
            token = data.get("nextPageToken")
            if not token:
                break
        return [v for v in self.videos(ids) if v.views]

    def channel(self, ref: str, limit: int, kinds: set[str]) -> Channel | None:
        ref = ref.strip()
        params = {}
        m = re.search(r"/channel/(UC[\w-]{22})", ref)
        if m or (ref.startswith("UC") and len(ref) == 24):
            params["id"] = m.group(1) if m else ref
        else:
            m = re.search(r"/@([^/?#]+)", ref)
            params["forHandle"] = "@" + (m.group(1) if m else ref.lstrip("@"))
        data = self._get("channels", 1, part="snippet,statistics,contentDetails", **params)
        if not data.get("items"):
            return None
        it = data["items"][0]
        st = it.get("statistics", {})
        ch = Channel(id=it["id"], title=it["snippet"].get("title", ""),
                     subscribers=None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0)))
        uploads = it["contentDetails"]["relatedPlaylists"]["uploads"]
        # В плейлисте загрузок Shorts и обычные ролики вперемешку — берём с запасом.
        want = limit * (2 if len(kinds) == 1 else 1)
        ids, token = [], None
        while len(ids) < want:
            pl = self._get("playlistItems", 1, part="contentDetails", playlistId=uploads,
                           maxResults=50, pageToken=token)
            ids += [x["contentDetails"]["videoId"] for x in pl.get("items", [])]
            token = pl.get("nextPageToken")
            if not token:
                break
        ch.videos = [v for v in self.videos(ids[:want])
                     if ("shorts" if v.is_short else "long") in kinds]
        return ch

    def enrich(self, videos: list[Video]) -> None:
        pass  # API и так отдаёт полные данные


# --------------------------------------------------------------------------- #
# Анализ
# --------------------------------------------------------------------------- #
def robust_z(value: float, sample: list[float]) -> float | None:
    if len(sample) < 3 or value <= 0:
        return None
    logs = [math.log10(max(x, 1)) for x in sample]
    med = statistics.median(logs)
    mad = statistics.median(abs(x - med) for x in logs) or 0.05
    return (math.log10(value) - med) / (1.4826 * mad)


def analyze(video: Video, ch: Channel | None, args, now: datetime) -> Result:
    kind_short = bool(video.is_short)
    base = []
    if ch:
        min_age = timedelta(days=args.baseline_min_age)
        for v in ch.videos:
            if v.id == video.id or not v.views or bool(v.is_short) != kind_short:
                continue
            if v.published and now - v.published < min_age:
                continue  # свежие ролики ещё не добрали просмотры и занижают медиану
            base.append(v.views)
    baseline = statistics.median(base) if len(base) >= args.min_baseline else None
    views = video.views or 0
    subs = ch.subscribers if ch else None
    age = (now - video.published).total_seconds() / 86400 if video.published else None
    return Result(
        video=video, subscribers=subs, baseline=baseline, baseline_n=len(base),
        score=views / max(baseline, 1) if baseline is not None else None,
        views_per_sub=views / subs if subs else None,
        age_days=age,
        views_per_day=views / max(age, 1 / 24) if age is not None else None,
        zscore=robust_z(views, base) if baseline is not None else None,
        **dict(zip(("ai_score", "ai_reasons"), ai_likelihood(video, ch, now))),
    )


# Явные упоминания ИИ: короткие аббревиатуры — с учётом регистра, остальное — без
_AI_CASE = re.compile(r"(?<![\w])(AI|A\.I\.|ИИ|ШІ|KI|IA)(?![\w])")
_AI_WORDS = re.compile(
    r"ai[- ]generated|generated (with|by|using) ai|made (with|by|using) ai|created (with|by|using) ai|"
    r"\bai (documentary|film|movie|video|animation|art|visuals?|narrat\w*|voice)|"
    r"artificial intelligence|inteligencia artificial|intelig[eê]ncia artificial|intelligence artificielle|"
    r"k[üu]nstliche intelligenz|нейросет|сгенерир|искусственн\w+ интеллект|"
    r"\b(veo ?3?|sora|kling|runway|midjourney|hailuo|pika labs|luma dream|elevenlabs|invideo|heygen)\b|"
    r"synthetic (content|media)|digitally generated|生成ai|ai生成|人工智能|인공지능|ai 생성", re.I)


def ai_likelihood(video: Video, ch: Channel | None, now: datetime) -> tuple[int, str]:
    """Эвристика «похоже на ИИ-канал». Не детектор: только косвенные признаки."""
    score, why = 0, []
    text = f"{video.title}\n{video.description}"
    if _AI_WORDS.search(text) or _AI_CASE.search(text):
        score += 45
        why.append("ИИ в названии/описании")
    if ch:
        longs = [v for v in ch.videos if not v.is_short]
        if any(_AI_WORDS.search(f"{v.title} {v.description}") or _AI_CASE.search(v.title)
               for v in longs if v.id != video.id):
            score += 15
            why.append("ИИ в других роликах канала")
        dated = sorted(v.published for v in longs if v.published)
        if len(dated) >= 4:
            span_w = max((dated[-1] - dated[0]).days / 7, 1)
            rate = (len(dated) - 1) / span_w
            long_share = sum(1 for v in longs if (v.duration or 0) >= 900) / max(len(longs), 1)
            if long_share >= 0.6 and rate >= 2:  # 2+ фильма по 15+ мин в неделю — конвейер
                pts = 30 if rate >= 5 else 20
                score += pts
                why.append(f"{rate:.1f} длинных роликов/нед")
            if len(longs) < 30 and (now - dated[0]).days <= 120:
                score += 15
                why.append("молодой канал")
        durs = [v.duration for v in longs if v.duration]
        if len(durs) >= 6 and statistics.median(durs) >= 900:
            cv = statistics.pstdev(durs) / statistics.mean(durs)
            if cv < 0.2:
                score += 10
                why.append("одинаковая длительность")
        if ch.subscribers is not None and ch.subscribers < 20000 and (video.views or 0) >= 100000:
            score += 5
            why.append("маленький канал, большой хит")
    return min(score, 100), ", ".join(why)


def passes(r: Result, a) -> bool:
    v = r.video
    title = v.title.lower()
    checks = [
        a.type == "all" or (a.type == "shorts") == bool(v.is_short),
        r.score is not None or a.keep_unscored,
        a.min_score is None or r.score is None or r.score >= a.min_score,
        a.min_views is None or (v.views or 0) >= a.min_views,
        a.max_views is None or (v.views or 0) <= a.max_views,
        a.min_subs is None or (r.subscribers is not None and r.subscribers >= a.min_subs),
        a.max_subs is None or (r.subscribers is not None and r.subscribers <= a.max_subs),
        a.min_views_per_sub is None or (r.views_per_sub is not None and r.views_per_sub >= a.min_views_per_sub),
        a.min_views_per_day is None or (r.views_per_day is not None and r.views_per_day >= a.min_views_per_day),
        a.min_zscore is None or (r.zscore is not None and r.zscore >= a.min_zscore),
        a.min_ai_score is None or r.ai_score >= a.min_ai_score,
        a.min_duration is None or (v.duration is not None and v.duration >= a.min_duration),
        a.max_duration is None or (v.duration is not None and v.duration <= a.max_duration),
        not a.title_include or any(w.lower() in title for w in a.title_include),
        not any(w.lower() in title for w in a.title_exclude or []),
    ]
    if a.max_age_days is not None:
        checks.append(r.age_days <= a.max_age_days if r.age_days is not None else not a.strict_dates)
    if a.min_age_days is not None:
        checks.append(r.age_days >= a.min_age_days if r.age_days is not None else not a.strict_dates)
    return all(checks)


SORT_KEYS = {
    "score": lambda r: r.score or 0,
    "views": lambda r: r.video.views or 0,
    "vps": lambda r: r.views_per_sub or 0,
    "vpd": lambda r: r.views_per_day or 0,
    "z": lambda r: r.zscore or -99,
    "ai": lambda r: (r.ai_score, r.score or 0),
}


# --------------------------------------------------------------------------- #
# Вывод
# --------------------------------------------------------------------------- #
def human(n) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}" if n == int(n) else f"{n:.2f}"


def fmt_duration(sec) -> str:
    if not sec:
        return "—"
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def print_table(results: list[Result]) -> None:
    if not results:
        print("Ничего не найдено по заданным критериям.")
        return
    head = f"{'#':>3} {'x-раз':>6} {'просм.':>7} {'медиана':>7} {'подп.':>7} {'пр/подп':>7} {'дней':>5} {'длит.':>7} {'ИИ':>3}  ролик"
    print(head)
    print("-" * min(len(head) + 60, 160))
    for i, r in enumerate(results, 1):
        v = r.video
        score = f"{r.score:.1f}" if r.score is not None else "—"
        age = f"{r.age_days:.0f}" if r.age_days is not None else "—"
        title = (v.title[:55] + "…") if len(v.title) > 56 else v.title
        print(f"{i:>3} {score:>6} {human(v.views):>7} {human(r.baseline):>7} {human(r.subscribers):>7} "
              f"{human(r.views_per_sub):>7} {age:>5} {fmt_duration(v.duration) if not v.is_short else 'shorts':>7} {r.ai_score:>3}  "
              f"{title} | {v.channel} | {v.url}")


def write_csv(results: list[Result], path: Path) -> None:
    rows = [r.row() for r in results]
    with path.open("w", newline="", encoding="utf-8-sig") as f:  # BOM — чтобы Excel понял кириллицу
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["score"], delimiter=";")
        w.writeheader()
        w.writerows(rows)


def write_json(results: list[Result], path: Path, meta: dict) -> None:
    path.write_text(json.dumps({"meta": meta, "results": [r.row() for r in results]},
                               ensure_ascii=False, indent=2), "utf-8")


def write_html(results: list[Result], path: Path, meta: dict) -> None:
    e = html.escape
    rows = []
    for i, r in enumerate(results, 1):
        d = r.row()
        cls = "hot" if (r.score or 0) >= 10 else "warm" if (r.score or 0) >= 3 else ""
        rows.append(
            f"<tr><td>{i}</td>"
            f"<td><a href='{e(d['url'])}' target=_blank><img loading=lazy src='https://i.ytimg.com/vi/{e(d['video_id'])}/mqdefault.jpg'></a></td>"
            f"<td class=t><a href='{e(d['url'])}' target=_blank>{e(d['title'])}</a><br>"
            f"<small>{e(d['channel'])} · {d['type']}{(' · <b class=ai>ИИ ' + str(d['ai_score']) + ':</b> ' + e(d['ai_reasons'])) if d['ai_score'] else ''}</small></td>"
            f"<td class='n {cls}' data-v='{d['score'] or 0}'><b>{'' if d['score'] is None else str(d['score']) + '×'}</b></td>"
            f"<td class=n data-v='{d['views'] or 0}'>{human(d['views'])}</td>"
            f"<td class=n data-v='{d['baseline_median'] or 0}'>{human(d['baseline_median'])}</td>"
            f"<td class=n data-v='{d['subscribers'] or 0}'>{human(d['subscribers'])}</td>"
            f"<td class=n data-v='{d['views_per_sub'] or 0}'>{human(d['views_per_sub'])}</td>"
            f"<td class=n data-v='{d['views_per_day'] or 0}'>{human(d['views_per_day'])}</td>"
            f"<td class=n data-v='{d['duration_sec'] or 0}'>{fmt_duration(d['duration_sec'])}</td>"
            f"<td class=n data-v='{d['age_days'] if d['age_days'] is not None else 99999}'>{d['published'] or '—'}</td>"
            f"<td class=n data-v='{d['ai_score']}' title='{e(d['ai_reasons'])}'>{d['ai_score'] or ''}</td></tr>")
    crit = " · ".join(f"{k}: {e(str(v))}" for k, v in meta["criteria"].items() if v not in (None, [], False))
    page = f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>YouTube аномалии</title><style>
:root{{--bg:#fff;--fg:#111;--mut:#666;--line:#e5e5e5;--hot:#d93025;--warm:#e37400;--head:#f6f6f6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121212;--fg:#eee;--mut:#9a9a9a;--line:#2a2a2a;--hot:#ff6b5e;--warm:#ffb347;--head:#1c1c1c}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif;margin:0 16px 40px}}
h1{{font-size:20px;margin:20px 0 4px}} .meta{{color:var(--mut);font-size:12px;margin-bottom:16px}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;min-width:900px}}
th,td{{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:middle}}
th{{background:var(--head);cursor:pointer;text-align:left;position:sticky;top:0;white-space:nowrap}}
td.n{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}
td.t{{max-width:420px}} small{{color:var(--mut)}} img{{width:160px;border-radius:6px;display:block}}
a{{color:inherit}} b.ai{{color:#7c4dff}} .hot b{{color:var(--hot)}} .warm b{{color:var(--warm)}}
</style></head><body>
<h1>Ролики с аномальными просмотрами — {len(results)}</h1>
<div class=meta>{e(meta['generated'])} · источник: {e(meta['backend'])} · {crit}</div>
<div class=wrap><table id=t><thead><tr><th>#</th><th></th><th>Ролик</th><th>× к медиане</th><th>Просмотры</th>
<th>Медиана канала</th><th>Подписчики</th><th>Просм./подп.</th><th>Просм./день</th><th>Длит.</th><th>Дата</th><th>ИИ?</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<script>
document.querySelectorAll('#t th').forEach((th,i)=>th.onclick=()=>{{
 const tb=document.querySelector('#t tbody'),rs=[...tb.rows],asc=th.dataset.asc!=='1';th.dataset.asc=asc?'1':'0';
 rs.sort((a,b)=>{{const x=a.cells[i],y=b.cells[i];const p=x.dataset.v,q=y.dataset.v;
  const r=p!==undefined?(+p)-(+q):x.innerText.localeCompare(y.innerText);return asc?r:-r}});
 rs.forEach(r=>tb.appendChild(r))}});
</script></body></html>"""
    path.write_text(page, "utf-8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Поиск роликов YouTube с аномальными просмотрами (outliers).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    g = p.add_argument_group("Что искать")
    g.add_argument("-q", "--query", action="append", default=[], help="поисковый запрос (можно несколько раз)")
    g.add_argument("--queries-file", help="файл с запросами, по одному на строку")
    g.add_argument("-c", "--channel", action="append", default=[], help="канал: @handle, UC… ID или URL (можно несколько)")
    g.add_argument("--channels-file", help="файл с каналами, по одному на строку")
    g.add_argument("--config", help="JSON-файл с критериями (ключи = названия опций, напр. min_score)")

    g = p.add_argument_group("Параметры поиска")
    g.add_argument("--limit", type=int, default=100, help="сколько роликов брать из поиска на каждый запрос")
    g.add_argument("--period", choices=list(PERIODS), help="фильтр поиска по дате загрузки")
    g.add_argument("--days", type=float, help="(api) загружено за последние N дней — точнее, чем --period")
    g.add_argument("--search-sort", nargs="+", choices=["relevance", "views", "date", "rating"],
                   default=["relevance"], help="сортировка выдачи поиска (можно несколько: relevance views)")
    g.add_argument("--search-duration", nargs="+", choices=["short", "medium", "long"],
                   help="фильтр поиска YouTube: short <4 мин, medium 4–20, long >20 (можно несколько)")
    g.add_argument("--region", help="(api) код страны, напр. RU, US")
    g.add_argument("--lang", help="(api) язык выдачи, напр. ru, en")

    g = p.add_argument_group("Критерии аномальности")
    g.add_argument("--type", choices=["all", "long", "shorts"], default="all", help="формат роликов")
    g.add_argument("--min-score", type=float, default=3.0, help="во сколько раз больше медианы канала")
    g.add_argument("--min-ai-score", type=int,
                   help="мин. оценка «похоже на ИИ» 0–100 (эвристика: упоминания ИИ, частота загрузок, молодой канал)")
    g.add_argument("--min-zscore", type=float, help="минимальный робастный z-score (≈3 — сильный выброс)")
    g.add_argument("--min-views", type=int, default=1000, help="минимум просмотров")
    g.add_argument("--max-views", type=int, help="максимум просмотров")
    g.add_argument("--min-subs", type=int, help="минимум подписчиков канала")
    g.add_argument("--max-subs", type=int, help="максимум подписчиков канала (ищем маленькие каналы)")
    g.add_argument("--min-views-per-sub", type=float, help="просмотров на подписчика, напр. 2")
    g.add_argument("--min-views-per-day", type=float, help="минимум просмотров в день")
    g.add_argument("--max-age-days", type=float, help="ролик не старше N дней")
    g.add_argument("--min-age-days", type=float, help="ролик не моложе N дней")
    g.add_argument("--strict-dates", action="store_true", help="отбрасывать ролики с неизвестной датой при фильтре по возрасту")
    g.add_argument("--min-duration", type=int, help="мин. длительность, сек")
    g.add_argument("--max-duration", type=int, help="макс. длительность, сек")
    g.add_argument("--title-include", action="append", default=[], help="в названии есть слово (любое из)")
    g.add_argument("--title-exclude", action="append", default=[], help="исключить, если в названии слово")
    g.add_argument("--max-per-channel", type=int, default=3, help="не больше N роликов с одного канала (0 = без лимита)")
    g.add_argument("--keep-unscored", action="store_true", help="оставлять ролики, для которых не удалось посчитать медиану")

    g = p.add_argument_group("База сравнения (медиана канала)")
    g.add_argument("--baseline-videos", type=int, default=30, help="сколько последних роликов канала брать для медианы")
    g.add_argument("--baseline-min-age", type=float, default=3, help="не брать в медиану ролики моложе N дней")
    g.add_argument("--min-baseline", type=int, default=5, help="минимум роликов для расчёта медианы")

    g = p.add_argument_group("Источник, вывод, прочее")
    g.add_argument("--backend", choices=["auto", "ytdlp", "api"], default="auto",
                   help="auto = api, если есть ключ, иначе ytdlp")
    g.add_argument("--api-key", default=os.environ.get("YOUTUBE_API_KEY"),
                   help="ключ YouTube Data API v3 (или переменная YOUTUBE_API_KEY)")
    g.add_argument("--cookies", help="(ytdlp) cookies.txt — если YouTube просит «подтвердите, что вы не бот»")
    g.add_argument("--cookies-from-browser", help="(ytdlp) взять cookies из браузера: chrome, firefox, edge…")
    g.add_argument("--enrich", action="store_true", help="(ytdlp) уточнить дату/лайки для найденных роликов (медленно)")
    g.add_argument("--sort", choices=list(SORT_KEYS), default="score", help="сортировка результата")
    g.add_argument("--top", type=int, default=50, help="сколько результатов показать/сохранить")
    g.add_argument("--out", default="results", help="папка для отчётов (HTML, CSV, JSON)")
    g.add_argument("--no-files", action="store_true", help="только вывод в консоль")
    g.add_argument("--workers", type=int, default=4, help="параллельных запросов к каналам")
    g.add_argument("--cache-ttl", type=float, default=12, help="кэш данных каналов, часов (0 = выкл.)")
    return p


def parse_args(argv=None):
    p = build_parser()
    pre, _ = p.parse_known_args(argv)
    if pre.config:
        cfg = json.loads(Path(pre.config).read_text("utf-8"))
        known = {a.dest for a in p._actions}
        bad = [k for k in cfg if k.replace("-", "_") not in known]
        if bad:
            p.error(f"неизвестные ключи в {pre.config}: {', '.join(bad)}")
        p.set_defaults(**{k.replace("-", "_"): v for k, v in cfg.items()})
    a = p.parse_args(argv)
    for attr, file_attr in (("query", "queries_file"), ("channel", "channels_file")):
        f = getattr(a, file_attr)
        if f:
            extra = [l.strip() for l in Path(f).read_text("utf-8").splitlines()
                     if l.strip() and not l.lstrip().startswith("#")]
            setattr(a, attr, list(getattr(a, attr)) + extra)
    if not a.query and not a.channel:
        p.error("укажите хотя бы один --query или --channel")
    return a


def main(argv=None) -> int:
    a = parse_args(argv)
    backend_name = a.backend if a.backend != "auto" else ("api" if a.api_key else "ytdlp")
    if backend_name == "api":
        if not a.api_key:
            sys.exit("Для --backend api нужен --api-key или переменная окружения YOUTUBE_API_KEY")
        be = ApiBackend(a.api_key)
    else:
        be = YtDlpBackend(a.cookies, a.cookies_from_browser)
    kinds = {"long", "shorts"} if a.type == "all" else {a.type}
    if a.type == "shorts" and not a.search_duration:
        a.search_duration = "short"  # фильтр поиска «до 4 минут»
    as_list = lambda x: [x] if isinstance(x, str) or x is None else list(x)  # noqa: E731
    durations, sorts = as_list(a.search_duration), as_list(a.search_sort)
    cache = Cache(Path(".cache") / "yt_outliers_channels.json", a.cache_ttl)
    now = datetime.now(timezone.utc)
    log(f"Источник данных: {backend_name}")

    # 1. Кандидаты
    candidates: dict[str, Video] = {}
    channels: dict[str, Channel] = {}
    jobs = [(q, d, s) for q in a.query for d in durations for s in sorts]

    def run_search(job):
        q, d, s = job
        try:
            return be.search(q, a.limit, a.period, d, s, days=a.days, region=a.region, lang=a.lang)
        except Exception as ex:
            log(f"  ! ошибка поиска «{q}»: {ex}")
            return []

    if jobs:
        log(f"Поисковых запросов: {len(jobs)} …")
        with ThreadPoolExecutor(a.workers) as ex:
            for i, ((q, d, s), found) in enumerate(zip(jobs, ex.map(run_search, jobs)), 1):
                new = sum(1 for v in found if v.id not in candidates)
                for v in found:
                    candidates.setdefault(v.id, v)
                log(f"  [{i}/{len(jobs)}] «{q}» {d or ''} {s}: {len(found)} роликов, новых {new}")

    def load_channel(ref: str, kinds: set[str] = kinds) -> Channel | None:
        key = f"{backend_name}|{ref}|{a.baseline_videos}|{','.join(sorted(kinds))}"
        cached = cache.get(key)
        if cached:
            return channel_from_json(cached)
        try:
            ch = be.channel(ref, a.baseline_videos, kinds)
        except Exception as ex:
            log(f"  ! канал {ref}: {ex}")
            return None
        if ch:
            cache.set(key, channel_to_json(ch))
        return ch

    # 2. Каналы, заданные явно: все их ролики — кандидаты
    if a.channel:
        log(f"Загружаю каналы: {len(a.channel)} …")
        with ThreadPoolExecutor(a.workers) as ex:
            for ref, ch in zip(a.channel, ex.map(load_channel, a.channel)):
                if not ch:
                    log(f"  ! не удалось загрузить канал {ref}")
                    continue
                channels[ch.id] = ch
                for v in ch.videos:
                    candidates.setdefault(v.id, v)

    # Отсекаем заведомо неподходящих кандидатов ДО загрузки каналов — экономит время
    def cheap_ok(v: Video) -> bool:
        t = v.title.lower()
        return ((a.min_views is None or (v.views or 0) >= a.min_views)
                and (a.max_views is None or (v.views or 0) <= a.max_views)
                and (a.min_duration is None or v.duration is None or v.duration >= a.min_duration)
                and (a.max_duration is None or v.duration is None or v.duration <= a.max_duration)
                and not (a.type == "long" and v.is_short and v.duration and v.duration <= 60)
                and (not a.title_include or any(w.lower() in t for w in a.title_include))
                and not any(w.lower() in t for w in a.title_exclude or []))

    before = len(candidates)
    candidates = {k: v for k, v in candidates.items() if v.channel_id in channels or cheap_ok(v)}
    if before != len(candidates):
        log(f"Предфильтр: осталось {len(candidates)} из {before} роликов")

    # 3. Базовые линии для каналов из поиска
    need = sorted({v.channel_id for v in candidates.values() if v.channel_id and v.channel_id not in channels})
    if need:
        log(f"Считаю медианы для каналов: {len(need)} (это самая долгая часть) …")
        done = 0
        with ThreadPoolExecutor(a.workers) as ex:
            # Для роликов из поиска грузим обе вкладки: так точно узнаём формат (Shorts/обычный).
            # Если нужны только длинные — хватит вкладки «Видео».
            both = {"long"} if a.type == "long" else {"long", "shorts"}
            for cid, ch in zip(need, ex.map(lambda c: load_channel(c, both), need)):
                done += 1
                if ch:
                    channels[cid] = ch
                if done % 10 == 0 or done == len(need):
                    log(f"  {done}/{len(need)}")
                    cache.save()
    cache.save()

    # Уточняем формат/дату кандидатов из поиска по данным канала
    for v in candidates.values():
        ch = channels.get(v.channel_id)
        if not ch:
            continue
        twin = next((x for x in ch.videos if x.id == v.id), None)
        if twin:
            v.is_short = twin.is_short if twin.is_short is not None else v.is_short
            v.published = v.published or twin.published
            v.duration = v.duration or twin.duration
        v.channel = v.channel or ch.title

    # 4. Анализ и фильтры
    results = [analyze(v, channels.get(v.channel_id), a, now) for v in candidates.values()]
    results = [r for r in results if passes(r, a)]
    results.sort(key=SORT_KEYS[a.sort], reverse=True)
    if a.max_per_channel:
        per: dict[str, int] = {}
        kept = []
        for r in results:
            per[r.video.channel_id] = per.get(r.video.channel_id, 0) + 1
            if per[r.video.channel_id] <= a.max_per_channel:
                kept.append(r)
        results = kept
    results = results[:a.top]

    if a.enrich and results:
        log(f"Уточняю данные {len(results)} роликов …")
        be.enrich([r.video for r in results])
        results = [analyze(r.video, channels.get(r.video.channel_id), a, now) for r in results]
        results = [r for r in results if passes(r, a)]
        results.sort(key=SORT_KEYS[a.sort], reverse=True)

    # 5. Вывод
    log(f"Проанализировано роликов: {len(candidates)}, каналов: {len(channels)}"
        + (f", квота API: ~{be.quota_used} ед." if backend_name == "api" else ""))
    print_table(results)
    if not a.no_files:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        stamp = now.astimezone().strftime("%Y%m%d_%H%M%S")
        crit_keys = ["query", "channel", "type", "period", "days", "min_score", "min_zscore", "min_views",
                     "max_views", "min_subs", "max_subs", "min_views_per_sub", "min_views_per_day",
                     "max_age_days", "min_age_days", "title_include", "title_exclude"]
        meta = {"generated": now.astimezone().strftime("%Y-%m-%d %H:%M"), "backend": backend_name,
                "analyzed_videos": len(candidates), "criteria": {k: getattr(a, k) for k in crit_keys}}
        base = out / f"outliers_{stamp}"
        write_csv(results, base.with_suffix(".csv"))
        write_json(results, base.with_suffix(".json"), meta)
        write_html(results, base.with_suffix(".html"), meta)
        log(f"Отчёты: {base}.html / .csv / .json")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
