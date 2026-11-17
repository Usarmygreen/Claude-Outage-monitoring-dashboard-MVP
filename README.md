# U.S. Outage Monitor — MVP

A local-first dashboard and polling bot for U.S.-relevant service outages.
FastAPI + SQLite + an asyncio background poller + a plain HTML/CSS/JS dashboard. No paid services, no API keys.

## What it does (and does not do)

**Implemented**
- Polls only the RSS/Atom feeds in `sources.json` (5 enabled vendor status feeds, 2 news feeds disabled by default).
- Stores every feed entry in SQLite and deduplicates on `(source, guid)`. A changed entry bumps its revision instead of creating a duplicate. Uses conditional GET (ETag / Last-Modified).
- Groups relevant entries into **incident candidates**. Every candidate starts as **unverified** with severity **unknown** and scope **unknown**.
- Operator review, protected by a token: confirm, attribute, dismiss, reopen, or set severity/scope/status. Confirming requires a primary source **or** two independent publishers. Every decision goes to an append-only audit log.
- Operators can attach a citation they checked by hand. The server never fetches operator-supplied URLs.
- Alerts (new primary signal, source updated, evidence or severity changed). Repeats increment a counter; acknowledgments are audited.
- Dashboard with separate Confirmed / Attributed / Unverified columns, filters, labels that use text and symbols as well as color, a detail view (timeline, sources, claim checks, alert history, audit log), and source freshness and error states. All times shown in U.S. Eastern.
- An optional, clearly labelled **research snapshot** (`seed_research.json`): three cited incidents from Nov 10–16, 2026, compiled on 2026-11-17. It is not live data.

**Design recommendations only (not implemented here)**
- FAA NAS status (`https://nasstatus.faa.gov/api/airport-status-information`) is reachable but serves XML, not RSS/Atom, so it is outside this MVP's RSS-only scope.
- Microsoft 365 Service Health requires your own tenant (Microsoft Graph, `ServiceHealth.Read.All`). The public Azure feed does not carry M365 incidents.
- Downdetector (no free API), Reddit (requires OAuth and acceptance of its API terms), and social platforms are not ingested.
- No outbound notifications (email, Teams, SMS), no map, no multi-user accounts, and no automated claim checking.

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime deps + pytest (Vercel installs requirements.txt only)
```

Optional environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `OPERATOR_TOKEN` | *(empty: review disabled)* | Long random secret required for review, citation, ack and manual poll actions |
| `ENABLE_POLLER` | `1` | Set `0` to serve stored data only |
| `LOAD_RESEARCH_SEED` | `1` | Set `0` to hide the research snapshot |
| `OUTAGE_DB` | `data/outages.db` | SQLite path |
| `CANDIDATE_LOOKBACK_DAYS` | `14` | Older feed entries are stored but not turned into candidates |
| `ALERT_MAX_AGE_HOURS` | `72` | No "new signal" alert for entries older than this |

Edit `user_agent` in `sources.json` to include a contact URL or email address.

## Run

```bash
export OPERATOR_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
uvicorn app:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

Bind to `127.0.0.1`. If you expose it on a network, put it behind TLS and real authentication; the shared token is MVP-grade.

## Verify

```bash
pytest -q                                   # expected: 15 passed (offline; no network used)
curl -s localhost:8000/api/status | python3 -m json.tool
```

Expected after about 1 minute: `last_checked` is set and each enabled source shows `"freshness": "fresh"`. If a source shows `"error"`, read `last_error`.
`GET /api/candidates?evidence_class=confirmed` returns only the three research-snapshot incidents until an operator confirms something.

What was actually tested by the author on 2026-11-17:
- The offline suite (15 tests) passed.
- A live poll from a cloud sandbox succeeded for all 7 listed feeds. On a second poll, every item was deduplicated and three hosts answered 304 Not Modified.
- Cloudflare's 25 maintenance entries produced no candidates.
- After a fix, a BleepingComputer phishing article was no longer flagged as a Microsoft outage.
- The dashboard rendered in headless Chromium with no console errors.

Your network may behave differently; check the Source freshness panel.

## Deploy to Vercel

The repo deploys as-is: Vercel detects the FastAPI `app` in `app.py` and installs `requirements.txt`.

1. Import the GitHub repo in Vercel (Framework preset: FastAPI, or "Other").
2. Optional: set `OPERATOR_TOKEN` (a long random secret) in the project's Environment Variables to enable review, acknowledgement and manual polling.
3. Deploy.

**What a serverless deployment does differently (by design):**
- The background poller is **off by default** (`VERCEL` is set automatically), because serverless functions do not run between requests. The dashboard shows "Poller off".
- SQLite lives in `/tmp`, which is **per instance and temporary**. The research snapshot reloads on every cold start; anything an operator records, and any feed items from a manual poll (`POST /api/poll` with the token), can disappear when the instance is recycled.
- For persistent, continuously polled monitoring, run it on a small always-on host (VM, container, home server) with `uvicorn`, or replace SQLite with a hosted database. Neither is included in this MVP.

## Safeguards

- **Anti-rumor:** news, forum and threat-intel items can only become *unverified* candidates. They are not alerted, and they appear in a visually separate dashed "Unverified signals" column. Nothing becomes "confirmed" without an operator and qualifying evidence.
- **SSRF:** only https URLs from `sources.json`. The host must be on the allowlist and resolve to public IPs, only the default port is allowed, redirects are re-validated (max 3), and there is no endpoint to add feeds. (DNS rebinding between the check and the connect is a known residual risk; pin IPs or use an egress proxy allowlist for higher assurance.)
- **Feed processing:** defusedxml rejects entity expansion and external entities. The body is capped at 2 MB after decompression. Markup and control characters are stripped. Links must be http(s).
- **Web:** strict Content-Security-Policy (no inline scripts or styles), `textContent`-only rendering, `nosniff`, `no-referrer`, and constant-time token comparison.
- **Prompt injection:** no language model reads feed content. Feed text is inert data and cannot change classifications. If you add an LLM summarizer later, treat feed text as untrusted input and never let it set evidence class or severity.
- **Politeness:** robots.txt is checked, there are at least 5 s between requests to a host, HTTP 429 `Retry-After` is honored, and failures back off exponentially (capped at 1 h).
- **Privacy:** only public feed metadata is stored (headline, link, time, short summary). No personal data, no cookies, no credentials.

## Adding a feed safely

1. Confirm it is an official RSS/Atom feed, open it in a browser, and read the publisher's feed terms.
2. Check `https://<host>/robots.txt` for the path.
3. Add an entry to `sources.json` with `tier` (`official` only for the provider's own status feed), `poll_minutes` of at least 5, and a `terms_note`.
4. Restart the server and watch its freshness state.

## Limitations

- Coverage is only what is listed. The absence of an alert means nothing about the rest of the internet.
- Clustering is heuristic: same feed, same normalized title and same ET day; news is grouped by organization and day. Operators should merge or dismiss duplicates.
- Statuspage feeds rarely state geography, so scope stays "unknown" until an operator sets it with a basis.
- The research snapshot is a dated record and will not update itself.
