"""Offline tests: no network access. Run with `pytest -q` from the project folder."""
import asyncio
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

os.environ["OUTAGE_NO_AUTOAPP"] = "1"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as m  # noqa: E402

ROOT = Path(m.__file__).parent
CFG = m.load_config(ROOT / "sources.json")
AWS = next(s for s in CFG["sources"] if s["key"] == "aws-health")
NEWS = {"key": "test-news", "name": "Test News", "tier": "news", "url": "https://news.example.com/feed",
        "organization": "", "service": "unspecified", "sector": "news"}

OFFICIAL_RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>AWS</title>
<item><guid>evt-1</guid><title>Service disruption: Increased Error Rates</title>
<link>https://status.aws.amazon.com/#evt1</link><pubDate>Mon, 16 Nov 2026 18:00:00 GMT</pubDate>
<description>&lt;b&gt;We are investigating&lt;/b&gt; increased error rates in US-EAST-1.</description></item>
<item><guid>evt-2</guid><title>Scheduled maintenance window</title><link>javascript:alert(1)</link>
<pubDate>Thu, 12 Nov 2026 19:00:00 GMT</pubDate><description>Scheduled maintenance.</description></item>
</channel></rss>"""

NEWS_RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>News</title>
<item><guid>n1</guid><title>Chase customers report outage</title><link>https://news.example.com/a</link>
<pubDate>Mon, 16 Nov 2026 19:00:00 GMT</pubDate><description>Users could not log in.</description></item>
<item><guid>n2</guid><title>Weather is nice</title><link>https://news.example.com/b</link>
<pubDate>Mon, 16 Nov 2026 19:00:00 GMT</pubDate><description>Sunny.</description></item>
</channel></rss>"""

XXE = b"""<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><title>&x;</title></channel></rss>"""


@pytest.fixture
def store(tmp_path):
    return m.Store(str(tmp_path / "t.db"))


@pytest.fixture
def client(tmp_path):
    a = m.create_app(db_path=str(tmp_path / "api.db"), seed_path=Path("/nonexistent"),
                     enable_poller=False, operator_token="s3cret-token")
    with TestClient(a) as c:
        yield c, a


def ingest(store, src, body):
    return m.ingest_entries(store, src, m.parse_feed(body), CFG, datetime(2026, 11, 17, tzinfo=timezone.utc))


def test_reporting_window_is_seven_complete_et_days():
    today = datetime(2026, 11, 17, 15, 58, tzinfo=m.ET)
    start, end = m.reporting_window(today)
    assert m.iso(start) == "2026-11-10T05:00:00+00:00"
    assert m.iso(end) == "2026-11-17T05:00:00+00:00"


def test_official_items_become_unverified_candidates_and_dedupe(store):
    s = ingest(store, AWS, OFFICIAL_RSS)
    assert s == {"new": 2, "updated": 0, "duplicate": 0, "candidates": 1}  # maintenance stored, not a candidate
    cands = store.q("SELECT * FROM candidates")
    assert len(cands) == 1 and cands[0]["evidence_class"] == "unverified" and cands[0]["severity"] == "unknown"
    assert cands[0]["scope"] == "unknown"
    item = store.one("SELECT * FROM source_items WHERE item_key='evt-1'")
    assert "<b>" not in item["summary"] and item["summary"].startswith("We are investigating")
    bad = store.one("SELECT url FROM source_items WHERE item_key='evt-2'")
    assert bad["url"] == AWS["url"]  # javascript: link replaced with the feed URL
    assert store.q("SELECT kind FROM alerts") == [{"kind": "new_primary_signal"}]
    # same content again -> pure duplicates, no new alerts
    assert ingest(store, AWS, OFFICIAL_RSS)["duplicate"] == 2
    assert len(store.q("SELECT * FROM alerts")) == 1
    # changed content -> revision bump + source_updated alert
    ingest(store, AWS, OFFICIAL_RSS.replace(b"investigating", b"mitigating"))
    assert store.one("SELECT revision FROM source_items WHERE item_key='evt-1'")["revision"] == 2
    assert {a["kind"] for a in store.q("SELECT kind FROM alerts")} == {"new_primary_signal", "source_updated"}


def test_old_items_are_stored_without_candidates_or_alerts(store):
    old = OFFICIAL_RSS.replace(b"Mon, 16 Nov 2026", b"Tue, 15 Sep 2026")
    s = ingest(store, AWS, old)
    assert s["new"] == 2 and s["candidates"] == 0
    assert store.q("SELECT * FROM alerts") == []


def test_gzip_feed_is_decoded_once(monkeypatch):
    import gzip
    _public_dns(monkeypatch)

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=gzip.compress(OFFICIAL_RSS), headers={"content-encoding": "gzip"})

    f = m.Fetcher("test", {"status.aws.amazon.com"}, transport=httpx.MockTransport(handler))

    async def run():
        r = await f.fetch("https://status.aws.amazon.com/rss/all.rss", None, None)
        await f.close()
        return r
    r = asyncio.run(run())
    assert len(m.parse_feed(r.content)) == 2


def test_news_signals_need_org_and_keyword_and_raise_no_alert(store):
    s = ingest(store, NEWS, NEWS_RSS)
    assert s["new"] == 2 and s["candidates"] == 1
    c = store.one("SELECT * FROM candidates")
    assert c["organization"] == "JPMorgan Chase" and c["evidence_class"] == "unverified"
    assert store.q("SELECT * FROM alerts") == []


def test_unsafe_xml_is_rejected():
    with pytest.raises(m.FetchError, match="unsafe XML"):
        m.parse_feed(XXE)


def test_ssrf_guard(monkeypatch):
    allowed = {"status.aws.amazon.com"}
    with pytest.raises(m.FetchError, match="non-https"):
        m.check_url("http://status.aws.amazon.com/rss/all.rss", allowed)
    with pytest.raises(m.FetchError, match="not in sources.json"):
        m.check_url("https://evil.example/x", allowed)
    with pytest.raises(m.FetchError, match="non-default port"):
        m.check_url("https://status.aws.amazon.com:8443/x", allowed)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(m.FetchError, match="non-public"):
        m.check_url("https://status.aws.amazon.com/rss/all.rss", allowed)


def _public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(m, "MIN_HOST_INTERVAL", 0)

    async def no_sleep(_s):
        return None
    monkeypatch.setattr(m.asyncio, "sleep", no_sleep)


def test_fetcher_blocks_cross_host_redirect_and_retries_5xx(monkeypatch):
    _public_dns(monkeypatch)
    calls = {"n": 0}

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "https://169.254.169.254/latest"})
        calls["n"] += 1
        return httpx.Response(503)

    f = m.Fetcher("test", {"status.aws.amazon.com"}, transport=httpx.MockTransport(handler))

    async def run():
        with pytest.raises(m.FetchError, match="not in sources.json"):
            await f.fetch("https://status.aws.amazon.com/redirect", None, None)
        with pytest.raises(m.FetchError, match="HTTP 503"):
            await f.fetch("https://status.aws.amazon.com/rss/all.rss", None, None)
        await f.close()
    asyncio.run(run())
    assert calls["n"] == m.RETRIES + 1


def test_fetcher_respects_robots_and_size_cap(monkeypatch):
    _public_dns(monkeypatch)

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, content=b"x" * (m.MAX_BYTES + 10))

    f = m.Fetcher("test", {"status.aws.amazon.com"}, transport=httpx.MockTransport(handler))

    async def run():
        with pytest.raises(m.FetchError, match="robots"):
            await f.fetch("https://status.aws.amazon.com/private/feed", None, None)
        with pytest.raises(m.FetchError, match="larger than 2 MB"):
            await f.fetch("https://status.aws.amazon.com/rss/all.rss", None, None)
        await f.close()
    asyncio.run(run())


def test_poll_failure_is_recorded(monkeypatch, store):
    _public_dns(monkeypatch)
    f = m.Fetcher("test", {"status.aws.amazon.com"}, transport=httpx.MockTransport(lambda r: httpx.Response(404)))

    async def run():
        res = await m.poll_source(store, f, AWS, CFG)
        await f.close()
        return res
    res = asyncio.run(run())
    assert "error" in res
    st = store.one("SELECT * FROM source_status WHERE source_key='aws-health'")
    assert st["consecutive_failures"] == 1 and "HTTP 404" in st["last_error"]


def test_review_requires_token_evidence_and_is_audited(client):
    c, a = client
    ingest(a.state.store, NEWS, NEWS_RSS)
    cid = c.get("/api/candidates").json()[0]["id"]
    review = {"operator": "dg", "action": "confirm", "note": "Checked Chase statement on CBS."}
    assert c.post(f"/api/candidates/{cid}/review", json=review).status_code == 401
    r = c.post(f"/api/candidates/{cid}/review", json=review, headers={"X-Operator-Token": "s3cret-token"})
    assert r.status_code == 422  # one news item is not enough to confirm
    cite = {"operator": "dg", "url": "https://www.cbsnews.com/news/x", "publisher": "CBS News", "title": "Bank outages",
            "tier": "news", "note": "Independent outlet quoting Chase."}
    assert c.post(f"/api/candidates/{cid}/citations", json=cite, headers={"X-Operator-Token": "s3cret-token"}).status_code == 200
    r = c.post(f"/api/candidates/{cid}/review", json={**review, "severity": "moderate"},
               headers={"X-Operator-Token": "s3cret-token"})
    assert r.status_code == 200 and r.json()["after"]["evidence_class"] == "confirmed"
    d = c.get(f"/api/candidates/{cid}").json()
    assert d["candidate"]["confidence"]["level"] == "high"
    assert [x["action"] for x in d["audit"]] == ["confirm", "add_citation"]
    assert {x["kind"] for x in d["alerts"]} == {"evidence_changed", "severity_changed"}


def test_operator_actions_disabled_without_token(tmp_path):
    a = m.create_app(db_path=str(tmp_path / "x.db"), seed_path=Path("/nonexistent"), enable_poller=False, operator_token="")
    with TestClient(a) as c:
        r = c.post("/api/poll")
        assert r.status_code == 403


def test_research_seed_loads_once_and_filters(tmp_path):
    a = m.create_app(db_path=str(tmp_path / "s.db"), seed_path=ROOT / "seed_research.json",
                     enable_poller=False, operator_token="")
    assert m.load_research_seed(a.state.store, ROOT / "seed_research.json") == 0  # idempotent
    with TestClient(a) as c:
        allc = c.get("/api/candidates").json()
        assert {x["organization"] for x in allc} == {"Microsoft", "FAA", "JPMorgan Chase"}
        assert all(x["evidence_class"] == "confirmed" for x in allc)
        crit = c.get("/api/candidates", params={"severity": "critical"}).json()
        assert [x["organization"] for x in crit] == ["FAA"]
        inwin = c.get("/api/candidates", params={"date_from": "2026-11-10", "date_to": "2026-11-16"}).json()
        assert len(inwin) == 3
        st = c.get("/api/status").json()
        assert st["research_seed_loaded"] and "ET" in st["window_label_et"]


def test_dashboard_served_with_csp(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "U.S. Outage Monitor" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert c.get("/api/candidates").json() == []  # empty state, nothing invented


def test_seed_file_is_consistent():
    data = json.loads((ROOT / "seed_research.json").read_text())
    for inc in data["incidents"]:
        ids = {s["id"] for s in inc["sources"]}
        assert all(t.get("source") in ids for t in inc["timeline"])
        assert all(c["source"] in ids for c in inc["claims"])
        assert inc["evidence_class"] in m.EVIDENCE and inc["severity"] in m.SEVERITIES
