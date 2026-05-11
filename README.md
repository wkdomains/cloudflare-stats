# cloudflare-stats

Small CLI for pulling referer, Web Analytics, and cache stats from Cloudflare.

`stats.py` currently supports three Cloudflare data sources:

- **Workers Observability**: request-log style Workers telemetry, including the raw
  `$workers.event.request.headers.referer` field.
- **Web Analytics**: RUM/Web Analytics GraphQL data, grouped by referer host/path
  with pageviews and visits.
- **Cache / HTTP Analytics**: zone-level GraphQL HTTP analytics for cache hit
  ratio, fully cache-served URLs, origin-pressure URLs, and cache status
  breakdowns.

The script uses only Python standard-library modules.

## Requirements

- Python 3.10+
- `CLOUDFLARE_API_TOKEN`
- A Cloudflare account ID, passed with `--account-id` or set as
  `CLOUDFLARE_ACCOUNT_ID`
- A Cloudflare zone ID for cache analytics, passed with `--zone-id` or set as
  `CLOUDFLARE_ZONE_ID`

```bash
export CLOUDFLARE_API_TOKEN=...
export CLOUDFLARE_ACCOUNT_ID=...
export CLOUDFLARE_ZONE_ID=...
```

Token permissions depend on which source you use:

- Workers Observability: `Account > Workers Observability > Write`
- Web Analytics GraphQL: `Account > Account Analytics > Read`
- Cache / HTTP Analytics GraphQL: `Account > Account Analytics > Read` and
  zone-resource access to the target zone
- Listing zones: `Zone > Zone > Read`
- Listing Web Analytics sites: token access to read Web Analytics/RUM site info
  for the account

If Cloudflare returns `Authentication error`, check that the token is the API
token secret, can access the account, has the source-specific scope above, and
does not have an IP allowlist blocking your current network.

## Usage

Run both data sources for the last 12 hours:

```bash
./stats.py
```

By default, `both` means Workers Observability plus Web Analytics. Cache
analytics is opt-in because it requires a zone ID.

Pass the account ID explicitly:

```bash
./stats.py --account-id 0c4da32fb09ef63e0149bde16a8af33f
```

Change the timeframe and number of rows:

```bash
./stats.py --timeframe 24h --limit 20
./stats.py --timeframe 30m --limit 5
./stats.py --timeframe 7d --limit 50
```

Supported timeframe suffixes are:

- `m` minutes
- `h` hours
- `d` days

## Data Sources

### Workers Observability

Workers Observability queries Cloudflare's Workers telemetry API:

```text
POST /accounts/{account_id}/workers/observability/telemetry/query
```

The default query:

- filters out empty `$workers.event.request.headers.referer`
- groups by `$workers.event.request.headers.referer`
- counts events
- sorts by count descending

Run only Workers Observability:

```bash
./stats.py --source workers
```

List unique referer values instead of counts:

```bash
./stats.py --source workers --values --limit 100
```

Use a custom Workers Observability dataset:

```bash
./stats.py --source workers --dataset cloudflare-workers
```

### Web Analytics

Web Analytics uses Cloudflare's GraphQL Analytics API:

```text
POST /client/v4/graphql
```

The query reads `rumPageloadEventsAdaptiveGroups` and prints:

- referer host/path
- pageviews
- visits

Run only Web Analytics:

```bash
./stats.py --source web-analytics
```

Filter Web Analytics by site tag:

```bash
./stats.py --source web-analytics --site-tag SITE_TAG
```

You can pass multiple site tags:

```bash
./stats.py --source web-analytics --site-tag SITE_TAG_1 --site-tag SITE_TAG_2
```

Or set a comma-separated environment variable:

```bash
export CLOUDFLARE_SITE_TAG=SITE_TAG_1,SITE_TAG_2
./stats.py --source web-analytics
```

Filter Web Analytics by request host:

```bash
./stats.py --source web-analytics --host example.com
```

By default, Web Analytics filters out likely bot traffic with `bot: 0`.
Include bot traffic with:

```bash
./stats.py --source web-analytics --include-bots
```

### Cache / HTTP Analytics

Cache analytics uses Cloudflare's GraphQL Analytics API at the zone level:

```text
POST /client/v4/graphql
```

The query reads `httpRequestsAdaptiveGroups` and can show:

- cache hit ratio by URL
- URLs served entirely from cache within the returned top-path window
- origin-pressure URLs, such as repeated `MISS`, `BYPASS`, `EXPIRED`,
  `DYNAMIC`, or `REVALIDATED`
- cache status breakdowns by URL

Find zones with recent HTTP data:

```bash
./stats.py --list-zones-with-data --timeframe 24h --limit 20
```

List zones without checking traffic:

```bash
./stats.py --list-zones
```

Run cache hit-ratio stats:

```bash
./stats.py --source cache --zone-id ZONE_ID
```

Show URLs that were fully cache-served in the returned top-path window:

```bash
./stats.py --source cache --zone-id ZONE_ID --cache-mode fully-cached
```

Show top cache-served URLs:

```bash
./stats.py --source cache --zone-id ZONE_ID --cache-mode cached
```

Show top origin-pressure URLs:

```bash
./stats.py --source cache --zone-id ZONE_ID --cache-mode origin
```

Show cache statuses by URL:

```bash
./stats.py --source cache --zone-id ZONE_ID --cache-mode statuses
```

Filter cache statuses:

```bash
./stats.py --source cache --zone-id ZONE_ID --cache-mode statuses --cache-status miss
./stats.py --source cache --zone-id ZONE_ID --cache-mode statuses --cache-status hit --cache-status stale
```

Filter cache analytics by host:

```bash
./stats.py --source cache --zone-id ZONE_ID --host example.com
```

`--cache-query-limit` controls the internal GraphQL row limit used to calculate
ratios. It defaults to `max(--limit * 10, 100)`.

## Listing Web Analytics Sites

Use this to find available Web Analytics `site_tag` values:

```bash
./stats.py --list-sites
```

With an explicit account ID:

```bash
./stats.py --list-sites --account-id 0c4da32fb09ef63e0149bde16a8af33f
```

## Token Verification

Check whether `CLOUDFLARE_API_TOKEN` is active:

```bash
./stats.py --verify-token
```

This only verifies the token itself. A token can verify successfully and still
fail a source query if it lacks that source's permission, lacks access to the
account, or is blocked by an IP allowlist.

## Raw JSON

Print raw API responses and per-source errors:

```bash
./stats.py --json
./stats.py --source web-analytics --json
./stats.py --source workers --json
```

## Output

Workers output is event counts by raw referer header:

```text
Workers Observability referers from 2026-05-11 00:00:00 UTC to 2026-05-11 12:00:00 UTC

#  count  referer
-  -----  ----------------------------------------
1     42  https://example.com/page
```

Web Analytics output is RUM pageviews and visits by referer host/path:

```text
Web Analytics referers from 2026-05-11 00:00:00 UTC to 2026-05-11 12:00:00 UTC

#  pageviews  visits  referer
-  ---------  ------  ----------------------------------------
1        123      87  google.com/search
```

Cache output is zone-level HTTP analytics by host/path:

```text
Cache hit ratio by URL from 2026-05-11 00:00:00 UTC to 2026-05-11 12:00:00 UTC

#  total  cached  origin  cache%   bytes  url
-  -----  ------  ------  ------  ------  ----------------------------------------
1    500     500       0  100.0%  2.4 MB  example.com/assets/app.js
```

## Notes

Workers Observability and Web Analytics are not identical data sets.

- Workers Observability is closer to raw request/event telemetry.
- Web Analytics is RUM/browser beacon analytics.
- Web Analytics may not include all traffic, especially clients that block or do
  not run the beacon.
- Web Analytics referers are grouped dimensions, not the full raw Workers
  `$workers.event.request.headers.referer` value.
- Web Analytics query strings are not available for attribution.
- Cache analytics is zone-level HTTP analytics, not Workers telemetry or RUM.
- Cache analytics uses sampled/adaptive GraphQL analytics. For exact per-request
  cache history, use Cloudflare logs/Logpush.
- `fully-cached` means the URL's returned top-path total was accounted for by
  cache-served statuses in the query window. Increase `--cache-query-limit` for
  broader coverage on busy zones.

When multiple sources are used, each source is queried independently. If one
source fails because of missing scope, missing zone ID, or unavailable data, the
script still prints other source results when available and reports the failed
source on stderr.
