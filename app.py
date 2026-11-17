"""U.S. outage monitor MVP: FastAPI + SQLite + an asyncio polling loop.

Safety model (see README "Safeguards"):
  * Feeds are fetched ONLY from https URLs listed in sources.json; nothing user-supplied is fetched.
  * Every fetch re-checks scheme, host allowlist and that the host resolves to public IPs (SSRF),
    follows at most 3 redirects (each re-checked), caps body size, and uses bounded timeouts/retries.
  * robots.txt is honored, a per-host minimum interval is enforced, and HTTP 429 Retry-After is respected.
  * XML is pre-parsed with defusedxml (rejects entity expansion / external entities) before feedparser.
  * Feed text is stored as plain text; the dashboard renders it with textContent only.
  * Nothing is auto-confirmed. Candidates start as 'unverified'; only an authenticated operator can change
    evidence class, severity or scope, and every change is written to an append-only audit log.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import socket
import sqlite3
import threading
import time
from calendar import timegm
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import feedparser
import httpx
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
MAX_BYTES = 2_000_000
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
RETRIES = 2                 # retries after the first attempt, for network errors and HTTP 5xx
MIN_HOST_INTERVAL = 5.0     # seconds between requests to one host
SCHEDULER_TICK = 60         # seconds between scheduler checks
CANDIDATE_LOOKBACK = timedelta(days=int(os.getenv("CANDIDATE_LOOKBACK_DAYS", "14")))  # older items: stored only
ALERT_MAX_AGE = timedelta(hours=int(os.getenv("ALERT_MAX_AGE_HOURS", "72")))           # no alerts for old news
log = logging.getLogger("outage-monitor")

SEVERITIES = ("unknown", "low", "moderate", "high", "critical")
EVIDENCE = ("unverified", "attributed", "confirmed", "dismissed")
SCOPES = ("unknown", "national", "multi_state", "state", "local")
STATUSES = ("unknown", "ongoing", "monitoring", "resolved")
AUTHORITATIVE = ("official", "agency")
_TAGS = re.compile(r"<[^>]+>")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAINT = re.compile(r"\b(scheduled|maintenance)\b", re.I)


class FetchError(Exception):
    pass


class RateLimited(FetchError):
    def __init__(self, retry_after_s: int):
        super().__init__(f"HTTP 429; retry after {retry_after_s}s")
        self.retry_after_s = retry_after_s


# --------------------------------------------------------------------------- helpers
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def plain(text: Optional[str], limit: int) -> str:
    """Strip markup and control characters; feed text is data, never HTML or instructions."""
    t = html.unescape(_TAGS.sub(" ", text or ""))
    t = _CTRL.sub("", re.sub(r"\s+", " ", t)).strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


def sha(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def safe_link(url: Optional[str], fallback: str) -> str:
    return url if url and urlsplit(url).scheme in ("http", "https") else fallback


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()[:120]


def reporting_window(today_et: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """Seven complete ET days before today: [today-7d 00:00 ET, today 00:00 ET)."""
    t = (today_et or now_utc().astimezone(ET)).date()
    end = datetime(t.year, t.month, t.day, tzinfo=ET)
    return end - timedelta(days=7), end


def load_config(path: Path) -> dict:
    cfg = json.loads(Path(path).read_text())
    for s in cfg["sources"]:
        u = urlsplit(s["url"])
        if u.scheme != "https" or not u.hostname or u.username or u.password:
            raise ValueError(f"source {s['key']}: only plain https URLs are allowed")
        s.setdefault("service", "unspecified")
        s.setdefault("organization", "")
    return cfg


# --------------------------------------------------------------------------- storage
class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript((BASE / "schema.sql").read_text())

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> Optional[dict]:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def audit(self, cid: Optional[int], actor: str, action: str, before, after, note: str) -> None:
        self.x("INSERT INTO audit_log(candidate_id,actor,action,before_json,after_json,note,at) VALUES (?,?,?,?,?,?,?)",
               (cid, actor, action, json.dumps(before) if before is not None else None,
                json.dumps(after) if after is not None else None, note, iso(now_utc())))

    def alert(self, cid: int, kind: str, message: str) -> None:
        now = iso(now_utc())
        with self.lock:
            self.conn.execute(
                "INSERT INTO alerts(candidate_id,kind,message,created_at,last_seen_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(candidate_id,kind,message) DO UPDATE SET occurrences=occurrences+1, last_seen_at=excluded.last_seen_at",
                (cid, kind, message, now, now))
            self.conn.commit()


# --------------------------------------------------------------------------- fetching
def check_url(url: str, allowed_hosts: set[str]) -> None:
    """SSRF guard: https only, allowlisted host, default port, public IPs only."""
    u = urlsplit(url)
    if u.scheme != "https":
        raise FetchError("blocked: non-https URL")
    if (u.hostname or "").lower() not in allowed_hosts:
        raise FetchError(f"blocked: host {u.hostname!r} is not in sources.json")
    if u.port not in (None, 443):
        raise FetchError("blocked: non-default port")
    try:
        infos = socket.getaddrinfo(u.hostname, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FetchError(f"DNS resolution failed: {e}") from e
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise FetchError("blocked: host resolves to a non-public address")


class Fetcher:
    def __init__(self, user_agent: str, allowed_hosts: set[str], transport=None):
        self.ua = user_agent
        self.allowed = allowed_hosts
        self.client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False, transport=transport,
                                        headers={"User-Agent": user_agent})
        self.last_hit: dict[str, float] = {}
        self.robots: dict[str, tuple[float, Optional[RobotFileParser]]] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def _pace(self, host: str) -> None:
        wait = MIN_HOST_INTERVAL - (time.monotonic() - self.last_hit.get(host, 0))
        if wait > 0:
            await asyncio.sleep(wait)
        self.last_hit[host] = time.monotonic()

    async def _get(self, url: str, headers: dict) -> httpx.Response:
        """GET with manual, re-validated redirects and a body-size cap. Returns a fully read response."""
        for _ in range(4):
            check_url(url, self.allowed)
            await self._pace(urlsplit(url).hostname)
            async with self.client.stream("GET", url, headers=headers) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, r.headers.get("location", ""))
                    continue
                body = bytearray()
                async for chunk in r.aiter_bytes():  # already decompressed, so the cap also limits gzip bombs
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise FetchError("blocked: response larger than 2 MB")
                # Body is decoded; drop encoding/length headers so httpx does not try to decode it again.
                headers = {k: v for k, v in r.headers.items() if k.lower() not in ("content-encoding", "content-length")}
                return httpx.Response(r.status_code, headers=headers, content=bytes(body), request=r.request)
        raise FetchError("too many redirects")

    async def allowed_by_robots(self, url: str) -> bool:
        u = urlsplit(url)
        origin = f"https://{u.hostname}"
        cached = self.robots.get(origin)
        if not cached or time.time() - cached[0] > 6 * 3600:
            rp: Optional[RobotFileParser] = None
            try:
                r = await self._get(origin + "/robots.txt", {})
                if r.status_code == 200:
                    rp = RobotFileParser()
                    rp.parse(r.text.splitlines())
            except (httpx.HTTPError, FetchError):
                rp = None  # unreachable/absent robots.txt is conventionally treated as allow
            self.robots[origin] = cached = (time.time(), rp)
        return cached[1] is None or cached[1].can_fetch(self.ua, url)

    async def fetch(self, url: str, etag: Optional[str], last_modified: Optional[str]) -> httpx.Response:
        if not await self.allowed_by_robots(url):
            raise FetchError("blocked by robots.txt")
        headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.1"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        for attempt in range(RETRIES + 1):
            try:
                r = await self._get(url, headers)
                if r.status_code == 429:
                    ra = r.headers.get("retry-after", "600")
                    raise RateLimited(int(ra) if ra.isdigit() else 600)
                if r.status_code >= 500:
                    raise FetchError(f"HTTP {r.status_code}")
                if r.status_code not in (200, 304):
                    raise FetchError(f"HTTP {r.status_code} (not retried)")
                return r
            except (httpx.TransportError, FetchError) as e:
                retryable = isinstance(e, httpx.TransportError) or str(e).startswith("HTTP 5")
                if not retryable or attempt == RETRIES:
                    raise
                await asyncio.sleep(2 ** attempt)
        raise FetchError("unreachable")


def parse_feed(body: bytes) -> list:
    try:
        SafeET.fromstring(body)  # forbids entity declarations and external references
    except DefusedXmlException as e:
        raise FetchError(f"blocked: unsafe XML ({type(e).__name__})") from e
    except SafeET.ParseError as e:
        raise FetchError(f"not well-formed XML: {e}") from e
    return feedparser.parse(body).entries


# --------------------------------------------------------------------------- ingestion
def match_orgs(text: str, aliases: dict[str, list[str]]) -> list[str]:
    low = text.lower()
    return [org for org, names in aliases.items()
            if any(re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", low) for n in names)]


def ingest_entries(store: Store, src: dict, entries: list, cfg: dict, fetched_at: datetime) -> dict:
    stats = {"new": 0, "updated": 0, "duplicate": 0, "candidates": 0}
    kw = [k.lower() for k in cfg.get("outage_keywords", [])]
    for e in entries:
        title = plain(e.get("title"), 300) or "(untitled)"
        summary = plain(e.get("summary") or e.get("description"), 600)
        st = e.get("published_parsed") or e.get("updated_parsed")
        published = iso(datetime.fromtimestamp(timegm(st), tz=timezone.utc)) if st else None
        link = safe_link(e.get("link"), src["url"])
        item_key = (e.get("id") or link or sha(title, published or ""))[:500]
        chash = sha(title, summary, published or "")
        prev = store.one("SELECT id, content_hash, revision, candidate_id FROM source_items WHERE source_key=? AND item_key=?",
                         (src["key"], item_key))
        if prev and prev["content_hash"] == chash:
            stats["duplicate"] += 1
            continue
        if prev:
            store.x("UPDATE source_items SET title=?, summary=?, url=?, published_at=?, fetched_at=?, content_hash=?, "
                    "revision=revision+1 WHERE id=?", (title, summary, link, published, iso(fetched_at), chash, prev["id"]))
            item_id, stats["updated"] = prev["id"], stats["updated"] + 1
            if prev["candidate_id"]:
                store.x("UPDATE candidates SET last_source_update=?, updated_at=? WHERE id=?",
                        (published or iso(fetched_at), iso(now_utc()), prev["candidate_id"]))
                store.alert(prev["candidate_id"], "source_updated",
                            f"{src['name']} updated: {title} (revision {prev['revision'] + 1})")
            continue
        item_id = store.x(
            "INSERT INTO source_items(source_key,item_key,tier,publisher,title,summary,url,published_at,fetched_at,content_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (src["key"], item_key, src["tier"], src["name"], title, summary, link, published, iso(fetched_at), chash))
        stats["new"] += 1

        item_time = parse_iso(published) or fetched_at
        if fetched_at - item_time > CANDIDATE_LOOKBACK:
            continue  # historical entry (e.g. first poll of a feed's archive): keep the record, no candidate
        blob = f"{title} {summary}"
        if src["tier"] in AUTHORITATIVE:
            if _MAINT.search(blob):
                continue  # stored for the record, but maintenance is not an incident candidate
            orgs = [src["organization"]] if src["organization"] else match_orgs(blob, cfg["org_aliases"])
        else:
            orgs = match_orgs(blob, cfg["org_aliases"])
            if not orgs or not any(re.search(rf"(?<![\w]){re.escape(k)}(?![\w])", blob.lower()) for k in kw):
                continue
        org = orgs[0] if orgs else "Unknown"
        day = (parse_iso(published) or fetched_at).astimezone(ET).strftime("%Y-%m-%d")
        key = (f"{src['key']}|{norm_title(title)}|{day}" if src["tier"] in AUTHORITATIVE else f"signal|{org}|{day}")
        cand = store.one("SELECT id FROM candidates WHERE cluster_key=?", (key,))
        if cand:
            cid = cand["id"]
            store.x("UPDATE candidates SET last_source_update=MAX(COALESCE(last_source_update,''),?), updated_at=? WHERE id=?",
                    (published or iso(fetched_at), iso(now_utc()), cid))
        else:
            reason = (f"Published on official status feed '{src['name']}'. Awaiting operator review."
                      if src["tier"] in AUTHORITATIVE else
                      f"{src['name']} item mentions {org} with outage wording. Unverified signal; needs corroboration.")
            now = iso(now_utc())
            cid = store.x(
                "INSERT INTO candidates(cluster_key,title,organization,service,sector,first_reported,last_source_update,"
                "inclusion_reason,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (key, title, org, src.get("service", "unspecified"), src.get("sector", "unknown"),
                 published or iso(fetched_at), published or iso(fetched_at), reason, now, now))
            stats["candidates"] += 1
            if src["tier"] in AUTHORITATIVE and fetched_at - item_time <= ALERT_MAX_AGE:
                store.alert(cid, "new_primary_signal", f"{src['name']}: {title}")
        store.x("UPDATE source_items SET candidate_id=? WHERE id=?", (cid, item_id))
        store.x("INSERT INTO timeline(candidate_id,event_at,text,evidence,source_item_id) VALUES (?,?,?,?,?)",
                (cid, published or iso(fetched_at), f"{src['name']} published: {title}",
                 "attributed" if src["tier"] in AUTHORITATIVE else "unverified", item_id))
    return stats


async def poll_source(store: Store, fetcher: Fetcher, src: dict, cfg: dict) -> dict:
    st = store.one("SELECT * FROM source_status WHERE source_key=?", (src["key"],)) or {}
    started = now_utc()
    store.x("INSERT INTO source_status(source_key,last_attempt) VALUES (?,?) "
            "ON CONFLICT(source_key) DO UPDATE SET last_attempt=excluded.last_attempt", (src["key"], iso(started)))
    try:
        r = await fetcher.fetch(src["url"], st.get("etag"), st.get("last_modified"))
        stats = {"not_modified": True} if r.status_code == 304 else ingest_entries(store, src, parse_feed(r.content), cfg, started)
        store.x("UPDATE source_status SET last_success=?, last_error=NULL, consecutive_failures=0, retry_after=NULL, "
                "etag=COALESCE(?,etag), last_modified=COALESCE(?,last_modified), items_seen=items_seen+? WHERE source_key=?",
                (iso(now_utc()), r.headers.get("etag"), r.headers.get("last-modified"), stats.get("new", 0), src["key"]))
        return stats
    except RateLimited as e:
        until = iso(now_utc() + timedelta(seconds=e.retry_after_s))
        store.x("UPDATE source_status SET last_error=?, consecutive_failures=consecutive_failures+1, retry_after=? "
                "WHERE source_key=?", (str(e), until, src["key"]))
        return {"error": str(e)}
    except (httpx.HTTPError, FetchError, ValueError) as e:
        msg = f"{type(e).__name__}: {e}"[:300]
        store.x("UPDATE source_status SET last_error=?, consecutive_failures=consecutive_failures+1 WHERE source_key=?",
                (msg, src["key"]))
        log.warning("poll %s failed: %s", src["key"], msg)
        return {"error": msg}


def due_sources(store: Store, cfg: dict, now: datetime) -> list[dict]:
    out = []
    for s in cfg["sources"]:
        if not s.get("enabled", True):
            continue
        st = store.one("SELECT last_attempt, retry_after, consecutive_failures FROM source_status WHERE source_key=?",
                       (s["key"],)) or {}
        if st.get("retry_after") and parse_iso(st["retry_after"]) > now:
            continue
        # back off on repeated failures: interval * 2^failures, capped at 1 hour
        interval = min(60, s.get("poll_minutes", 10) * (2 ** min(st.get("consecutive_failures") or 0, 4)))
        last = parse_iso(st.get("last_attempt"))
        if not last or now - last >= timedelta(minutes=interval):
            out.append(s)
    return out


async def poll_due(store: Store, fetcher: Fetcher, cfg: dict) -> dict:
    results = {}
    for s in due_sources(store, cfg, now_utc()):
        results[s["key"]] = await poll_source(store, fetcher, s, cfg)
    return results


# --------------------------------------------------------------------------- research seed
def load_research_seed(store: Store, path: Path) -> int:
    """Load analyst-researched incidents (cited, dated). Idempotent by cluster_key."""
    if not path.exists():
        return 0
    data = json.loads(path.read_text())
    n = 0
    for inc in data["incidents"]:
        key = f"research|{inc['slug']}"
        if store.one("SELECT id FROM candidates WHERE cluster_key=?", (key,)):
            continue
        now = iso(now_utc())
        cid = store.x(
            "INSERT INTO candidates(cluster_key,title,organization,service,sector,evidence_class,severity,scope,scope_basis,"
            "scope_detail,status,event_start,event_start_approx,event_end,event_end_approx,first_reported,last_source_update,"
            "inclusion_reason,impact,origin,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, inc["title"], inc["organization"], inc["service"], inc["sector"], inc["evidence_class"], inc["severity"],
             inc["scope"], inc["scope_basis"], inc["scope_detail"], inc["status"], inc.get("event_start"),
             int(inc.get("event_start_approx", False)), inc.get("event_end"), int(inc.get("event_end_approx", False)),
             inc.get("first_reported"), inc.get("last_source_update"), inc["inclusion_reason"], inc["impact"],
             "analyst_research", now, now))
        ids = {}
        for s in inc["sources"]:
            ids[s["id"]] = store.x(
                "INSERT INTO source_items(source_key,item_key,tier,publisher,title,summary,url,published_at,fetched_at,"
                "content_hash,candidate_id) VALUES ('analyst',?,?,?,?,?,?,?,NULL,?,?)",
                (f"{inc['slug']}|{s['id']}", s["tier"], s["publisher"], s["title"], s.get("note", ""), s["url"],
                 s.get("published_at"), sha(s["url"]), cid))
        for t in inc["timeline"]:
            store.x("INSERT INTO timeline(candidate_id,event_at,approximate,text,evidence,source_item_id) VALUES (?,?,?,?,?,?)",
                    (cid, t["at"], int(t.get("approx", False)), t["text"], t["evidence"], ids.get(t.get("source"))))
        for c in inc["claims"]:
            store.x("INSERT INTO claims(candidate_id,claim,claimant,claim_date,verdict,rationale,source_item_id) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (cid, c["claim"], c["claimant"], c.get("date"), c["verdict"], c["rationale"], ids.get(c.get("source"))))
        store.audit(cid, data["meta"]["analyst"], "seed_research", None,
                    {"evidence_class": inc["evidence_class"], "severity": inc["severity"]},
                    f"Loaded from research snapshot compiled {data['meta']['compiled_at']}. Re-verify sources before relying on it.")
        n += 1
    return n


# --------------------------------------------------------------------------- assessment
def independent_publishers(items: list[dict]) -> int:
    """Count independent non-official publishers; syndicated copies (same normalized title) count once."""
    seen_titles, domains = set(), set()
    for i in items:
        if i["tier"] not in ("news",):
            continue
        t, d = norm_title(i["title"]), (urlsplit(i["url"]).hostname or "").removeprefix("www.")
        if t in seen_titles or d in domains:
            continue
        seen_titles.add(t)
        domains.add(d)
    return len(domains)


def assess_confidence(cand: dict, items: list[dict], claims: list[dict]) -> dict:
    reasons = []
    has_primary = any(i["tier"] in AUTHORITATIVE for i in items)
    indep = independent_publishers(items)
    if has_primary:
        reasons.append("A primary source (official status page or agency) is attached.")
    if indep:
        reasons.append(f"{indep} independent news publisher(s); syndicated copies counted once.")
    if cand["evidence_class"] == "confirmed" and (has_primary or indep >= 2):
        level = "high"
    elif has_primary or indep >= 2:
        level = "medium"
    else:
        level = "low"
        reasons.append("No primary source and fewer than two independent publishers.")
    if any(c["verdict"] in ("contradicted", "unresolved") for c in claims):
        reasons.append("Some claims about this incident are contradicted or unresolved (see claim checks).")
    last = parse_iso(cand.get("last_source_update"))
    if cand["status"] in ("ongoing", "unknown") and last and now_utc() - last > timedelta(hours=72):
        reasons.append("Newest evidence is more than 72 hours old; status may be stale.")
        if level == "high":
            level = "medium"
    return {"level": level, "reasons": reasons, "has_primary": has_primary, "independent_publishers": indep}


# --------------------------------------------------------------------------- API
class Review(BaseModel):
    operator: str = Field(min_length=2, max_length=60)
    action: Literal["confirm", "attribute", "dismiss", "reopen", "update"]
    note: str = Field(min_length=10, max_length=2000)
    severity: Optional[Literal[SEVERITIES]] = None  # type: ignore[valid-type]
    scope: Optional[Literal[SCOPES]] = None  # type: ignore[valid-type]
    scope_basis: Optional[Literal["unknown", "reported", "confirmed"]] = None
    scope_detail: Optional[str] = Field(default=None, max_length=300)
    status: Optional[Literal[STATUSES]] = None  # type: ignore[valid-type]


class Citation(BaseModel):
    operator: str = Field(min_length=2, max_length=60)
    url: str = Field(max_length=1000)
    publisher: str = Field(min_length=2, max_length=120)
    title: str = Field(min_length=3, max_length=300)
    tier: Literal["official", "agency", "news", "forum", "threat_intel", "tracker"]
    published_at: Optional[datetime] = None
    note: str = Field(min_length=10, max_length=1000)


class Ack(BaseModel):
    operator: str = Field(min_length=2, max_length=60)


def create_app(db_path: Optional[str] = None, sources_path: Optional[Path] = None,
               seed_path: Optional[Path] = None, enable_poller: Optional[bool] = None,
               operator_token: Optional[str] = None) -> FastAPI:
    on_vercel = bool(os.getenv("VERCEL"))  # serverless: read-only bundle, only /tmp is writable, no long-lived process
    db_path = db_path or os.getenv("OUTAGE_DB", "/tmp/outages.db" if on_vercel else str(BASE / "data" / "outages.db"))
    cfg = load_config(sources_path or BASE / "sources.json")
    seed_path = seed_path if seed_path is not None else (
        BASE / "seed_research.json" if os.getenv("LOAD_RESEARCH_SEED", "1") == "1" else Path("/nonexistent"))
    enable_poller = enable_poller if enable_poller is not None else os.getenv("ENABLE_POLLER", "0" if on_vercel else "1") == "1"
    token = operator_token if operator_token is not None else os.getenv("OPERATOR_TOKEN", "")
    store = Store(db_path)
    allowed = {urlsplit(s["url"]).hostname.lower() for s in cfg["sources"]}
    state: dict = {"seeded": load_research_seed(store, seed_path)}

    async def scheduler(stop: asyncio.Event):
        fetcher = Fetcher(cfg["user_agent"], allowed)
        try:
            while not stop.is_set():
                try:
                    await poll_due(store, fetcher, cfg)
                except Exception:  # keep the loop alive
                    log.exception("poll cycle failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=SCHEDULER_TICK)
                except asyncio.TimeoutError:
                    pass
        finally:
            await fetcher.close()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler(stop)) if enable_poller else None
        yield
        stop.set()
        if task:
            await asyncio.wait([task], timeout=15)

    app = FastAPI(title="U.S. Outage Monitor MVP", lifespan=lifespan)
    app.state.store, app.state.cfg = store, cfg

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; style-src 'self'; "
                                                   "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                                                   "base-uri 'none'; form-action 'self'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    def require_operator(x_operator_token: str) -> None:
        if not token:
            raise HTTPException(403, "Operator actions are disabled. Set OPERATOR_TOKEN to enable them.")
        if not hmac.compare_digest(x_operator_token.encode(), token.encode()):
            raise HTTPException(401, "Invalid operator token.")

    def detail_parts(cid: int):
        items = store.q("SELECT * FROM source_items WHERE candidate_id=? ORDER BY COALESCE(published_at,fetched_at)", (cid,))
        claims = store.q("SELECT * FROM claims WHERE candidate_id=? ORDER BY id", (cid,))
        return items, claims

    @app.get("/api/status")
    def status():
        start, end = reporting_window()
        rows = {r["source_key"]: r for r in store.q("SELECT * FROM source_status")}
        now = now_utc()
        sources = []
        for s in cfg["sources"]:
            r = rows.get(s["key"], {})
            last_ok = parse_iso(r.get("last_success"))
            fresh = "disabled" if not s.get("enabled", True) else (
                "never_polled" if not r.get("last_attempt") else
                "error" if r.get("last_error") and (not last_ok or parse_iso(r["last_attempt"]) > last_ok) else
                "stale" if last_ok and now - last_ok > timedelta(minutes=3 * s.get("poll_minutes", 10)) else "fresh")
            sources.append({"key": s["key"], "name": s["name"], "tier": s["tier"], "url": s["url"],
                            "enabled": s.get("enabled", True), "poll_minutes": s.get("poll_minutes"),
                            "terms_note": s.get("terms_note", ""), "freshness": fresh,
                            "last_attempt": r.get("last_attempt"), "last_success": r.get("last_success"),
                            "last_error": r.get("last_error"), "consecutive_failures": r.get("consecutive_failures", 0)})
        attempts = [x["last_attempt"] for x in sources if x["last_attempt"]]
        return {"server_time": iso(now), "window_start": iso(start), "window_end": iso(end),
                "window_label_et": f"{start:%a %b %d, %Y} 00:00 ET to {end:%a %b %d, %Y} 00:00 ET (end exclusive)",
                "last_checked": max(attempts) if attempts else None, "poller_enabled": enable_poller,
                "operator_actions_enabled": bool(token), "research_seed_loaded": state["seeded"] > 0
                or bool(store.one("SELECT 1 FROM candidates WHERE origin='analyst_research'")),
                "geo_note": "No map: this MVP has no verified location data. Scope is shown as text with its basis.",
                "sources": sources}

    @app.get("/api/candidates")
    def candidates(evidence_class: Optional[str] = None, severity: Optional[str] = None,
                   confidence: Optional[str] = None, sector: Optional[str] = None, status: Optional[str] = None,
                   q: Optional[str] = Query(None, max_length=100), date_from: Optional[str] = None,
                   date_to: Optional[str] = None, include_dismissed: bool = False):
        out = []
        for c in store.q("SELECT * FROM candidates ORDER BY COALESCE(event_start, first_reported) DESC"):
            if c["evidence_class"] == "dismissed" and not include_dismissed and evidence_class != "dismissed":
                continue
            if evidence_class and c["evidence_class"] != evidence_class:
                continue
            if severity and c["severity"] != severity:
                continue
            if sector and c["sector"] != sector:
                continue
            if status and c["status"] != status:
                continue
            if q and q.lower() not in f"{c['title']} {c['organization']} {c['service']}".lower():
                continue
            anchor = parse_iso(c["event_start"] or c["first_reported"])
            anchor_day = anchor.astimezone(ET).strftime("%Y-%m-%d") if anchor else None
            if date_from and (not anchor_day or anchor_day < date_from):
                continue
            if date_to and (not anchor_day or anchor_day > date_to):
                continue
            items, claims = detail_parts(c["id"])
            conf = assess_confidence(c, items, claims)
            if confidence and conf["level"] != confidence:
                continue
            c["confidence"] = conf
            c["source_count"] = len(items)
            c["source_tiers"] = sorted({i["tier"] for i in items})
            c["open_alerts"] = store.one("SELECT COUNT(*) n FROM alerts WHERE candidate_id=? AND acknowledged_at IS NULL",
                                         (c["id"],))["n"]
            out.append(c)
        return out

    @app.get("/api/candidates/{cid}")
    def candidate(cid: int):
        c = store.one("SELECT * FROM candidates WHERE id=?", (cid,))
        if not c:
            raise HTTPException(404, "not found")
        items, claims = detail_parts(cid)
        c["confidence"] = assess_confidence(c, items, claims)
        return {"candidate": c, "sources": items, "claims": claims,
                "timeline": store.q("SELECT * FROM timeline WHERE candidate_id=? ORDER BY event_at, id", (cid,)),
                "alerts": store.q("SELECT * FROM alerts WHERE candidate_id=? ORDER BY last_seen_at DESC", (cid,)),
                "audit": store.q("SELECT * FROM audit_log WHERE candidate_id=? ORDER BY id DESC", (cid,))}

    @app.get("/api/alerts")
    def alerts(limit: int = Query(100, le=500)):
        return store.q("SELECT a.*, c.title, c.organization, c.evidence_class FROM alerts a JOIN candidates c "
                       "ON c.id=a.candidate_id ORDER BY a.last_seen_at DESC LIMIT ?", (limit,))

    @app.post("/api/candidates/{cid}/review")
    def review(cid: int, body: Review, x_operator_token: str = Header(default="")):
        require_operator(x_operator_token)
        c = store.one("SELECT * FROM candidates WHERE id=?", (cid,))
        if not c:
            raise HTTPException(404, "not found")
        items, claims = detail_parts(cid)
        conf = assess_confidence(c, items, claims)
        strong = conf["has_primary"] or conf["independent_publishers"] >= 2
        if body.action == "confirm" and not strong:
            raise HTTPException(422, "Cannot confirm: attach a primary (official/agency) source or two independent "
                                     "publishers first.")
        if body.scope_basis == "confirmed" and not strong:
            raise HTTPException(422, "Cannot mark scope as confirmed without primary or independent evidence.")
        fields = ("evidence_class", "severity", "scope", "scope_basis", "scope_detail", "status")
        before = {k: c[k] for k in fields}
        after = dict(before)
        after["evidence_class"] = {"confirm": "confirmed", "attribute": "attributed", "dismiss": "dismissed",
                                   "reopen": "unverified", "update": c["evidence_class"]}[body.action]
        for k in ("severity", "scope", "scope_basis", "scope_detail", "status"):
            if getattr(body, k) is not None:
                after[k] = getattr(body, k)
        store.x("UPDATE candidates SET evidence_class=?, severity=?, scope=?, scope_basis=?, scope_detail=?, status=?, "
                "updated_at=? WHERE id=?", (*[after[k] for k in fields], iso(now_utc()), cid))
        store.audit(cid, f"operator:{body.operator}", body.action, before, after, body.note)
        if after["evidence_class"] != before["evidence_class"]:
            store.alert(cid, "evidence_changed", f"Evidence class {before['evidence_class']} -> {after['evidence_class']}")
        if after["severity"] != before["severity"]:
            store.alert(cid, "severity_changed", f"Severity {before['severity']} -> {after['severity']}")
        return {"ok": True, "before": before, "after": after}

    @app.post("/api/candidates/{cid}/citations")
    def add_citation(cid: int, body: Citation, x_operator_token: str = Header(default="")):
        """Record a source an operator checked by hand. The server never fetches this URL."""
        require_operator(x_operator_token)
        if not store.one("SELECT id FROM candidates WHERE id=?", (cid,)):
            raise HTTPException(404, "not found")
        if urlsplit(body.url).scheme not in ("http", "https"):
            raise HTTPException(422, "Only http(s) links are accepted.")
        item_id = store.x(
            "INSERT INTO source_items(source_key,item_key,tier,publisher,title,summary,url,published_at,fetched_at,"
            "content_hash,candidate_id) VALUES ('operator',?,?,?,?,?,?,?,NULL,?,?)",
            (sha(str(cid), body.url), body.tier, plain(body.publisher, 120), plain(body.title, 300),
             plain(body.note, 600), body.url, iso(body.published_at), sha(body.url), cid))
        store.audit(cid, f"operator:{body.operator}", "add_citation", None,
                    {"url": body.url, "tier": body.tier}, body.note)
        return {"ok": True, "source_item_id": item_id}

    @app.post("/api/alerts/{aid}/ack")
    def ack(aid: int, body: Ack, x_operator_token: str = Header(default="")):
        require_operator(x_operator_token)
        a = store.one("SELECT * FROM alerts WHERE id=?", (aid,))
        if not a:
            raise HTTPException(404, "not found")
        store.x("UPDATE alerts SET acknowledged_by=?, acknowledged_at=? WHERE id=?",
                (body.operator, iso(now_utc()), aid))
        store.audit(a["candidate_id"], f"operator:{body.operator}", "ack_alert", None, {"alert_id": aid},
                    f"Acknowledged alert: {a['message'][:200]}")
        return {"ok": True}

    @app.post("/api/poll")
    async def poll_now(x_operator_token: str = Header(default="")):
        require_operator(x_operator_token)
        fetcher = Fetcher(cfg["user_agent"], allowed)
        try:
            results = {}
            for s in cfg["sources"]:
                if s.get("enabled", True):
                    results[s["key"]] = await poll_source(store, fetcher, s, cfg)
            return results
        finally:
            await fetcher.close()

    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

    @app.get("/")
    def index():
        return FileResponse(BASE / "static" / "index.html")

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app = create_app() if os.getenv("OUTAGE_NO_AUTOAPP") != "1" else None
