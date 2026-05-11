#!/usr/bin/env python3
"""Query Cloudflare stats from Workers Observability, Web Analytics, and cache analytics."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


API_BASE = "https://api.cloudflare.com/client/v4"
REFERER_KEY = "$workers.event.request.headers.referer"
DEFAULT_DATASET = "cloudflare-workers"
CACHE_SERVED_STATUSES = ("hit", "stale", "updating")
ORIGIN_STATUSES = ("miss", "expired", "bypass", "dynamic", "revalidated")
AUTO_TIMEFRAMES = {
    "workers": ("7d", "3d", "24h", "12h"),
    "web_analytics": ("180d", "90d", "30d", "7d", "24h"),
    "cache": ("30d", "7d", "72h", "24h"),
}
NON_TIMEFRAME_ERROR_HINTS = (
    "authentication",
    "does not have permission",
    "does not have access to the field",
    "invalid request headers",
    "unknown field",
)


@dataclass(frozen=True)
class RefererCount:
    referer: str
    count: int | float


@dataclass(frozen=True)
class WebAnalyticsRefererCount:
    referer: str
    pageviews: int | float
    visits: int | float


@dataclass(frozen=True)
class ZoneInfo:
    zone_id: str
    name: str
    status: str
    plan: str


@dataclass(frozen=True)
class ZoneTraffic:
    zone_id: str
    name: str
    requests: int | float
    bytes_sent: int | float
    visits: int | float


@dataclass(frozen=True)
class ZoneScanError:
    zone_id: str
    name: str
    message: str


@dataclass(frozen=True)
class CachePathStats:
    host: str
    path: str
    total: int | float
    cached: int | float
    origin: int | float
    bytes_sent: int | float

    @property
    def url(self) -> str:
        if self.host:
            return f"{self.host}{self.path}"
        return self.path

    @property
    def hit_ratio(self) -> float:
        if not self.total:
            return 0.0
        return float(self.cached) / float(self.total)


@dataclass(frozen=True)
class CacheStatusStats:
    host: str
    path: str
    status: str
    count: int | float
    bytes_sent: int | float
    response_status: int | float | None

    @property
    def url(self) -> str:
        if self.host:
            return f"{self.host}{self.path}"
        return self.path


class CloudflareAPIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show Cloudflare referer and cache stats.",
    )
    parser.add_argument(
        "--account-id",
        default=os.environ.get("CLOUDFLARE_ACCOUNT_ID"),
        help="Cloudflare account ID. Defaults to CLOUDFLARE_ACCOUNT_ID.",
    )
    parser.add_argument(
        "--timeframe",
        help=(
            "Relative time window such as 30m, 12h, 7d. "
            "Defaults to an automatic source-specific maximum."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of referers to display. Defaults to 10.",
    )
    parser.add_argument(
        "--dataset",
        default=DEFAULT_DATASET,
        help=f"Observability dataset. Defaults to {DEFAULT_DATASET}.",
    )
    parser.add_argument(
        "--source",
        choices=("workers", "web-analytics", "cache", "both", "all"),
        default="both",
        help=(
            "Data source to query. 'both' means workers and web-analytics; "
            "'all' also queries cache and requires --zone-id. Defaults to both."
        ),
    )
    parser.add_argument(
        "--zone-id",
        default=os.environ.get("CLOUDFLARE_ZONE_ID"),
        help="Cloudflare zone ID for cache analytics. Defaults to CLOUDFLARE_ZONE_ID.",
    )
    parser.add_argument(
        "--site-tag",
        action="append",
        default=[],
        help=(
            "Web Analytics site_tag to filter by. Can be passed multiple times. "
            "Defaults to CLOUDFLARE_SITE_TAG if set, otherwise all sites visible to the token."
        ),
    )
    parser.add_argument(
        "--host",
        help="Web Analytics requestHost filter, such as example.com.",
    )
    parser.add_argument(
        "--include-bots",
        action="store_true",
        help="Include likely bot traffic in Web Analytics results.",
    )
    parser.add_argument(
        "--list-sites",
        action="store_true",
        help="List Web Analytics sites and exit.",
    )
    parser.add_argument(
        "--list-zones",
        action="store_true",
        help="List zones for the account and exit.",
    )
    parser.add_argument(
        "--list-zones-with-data",
        action="store_true",
        help="List zones ranked by recent HTTP request volume and exit.",
    )
    parser.add_argument(
        "--zone-scan-limit",
        type=int,
        default=100,
        help="Maximum zones to scan for --list-zones-with-data. Defaults to 100.",
    )
    parser.add_argument(
        "--cache-mode",
        choices=("hit-ratio", "fully-cached", "cached", "origin", "statuses"),
        default="hit-ratio",
        help="Cache analytics view for --source cache. Defaults to hit-ratio.",
    )
    parser.add_argument(
        "--cache-status",
        action="append",
        default=[],
        help=(
            "Cache status filter for --cache-mode statuses, such as hit, miss, "
            "bypass, dynamic, expired, revalidated. Can be passed multiple times."
        ),
    )
    parser.add_argument(
        "--cache-query-limit",
        type=int,
        default=0,
        help=(
            "Internal GraphQL row limit for cache calculations. Defaults to a "
            "larger value based on --limit."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw Cloudflare response JSON.",
    )
    parser.add_argument(
        "--values",
        action="store_true",
        help="List unique referer values instead of top referers by count.",
    )
    parser.add_argument(
        "--verify-token",
        action="store_true",
        help="Only verify CLOUDFLARE_API_TOKEN and print its status.",
    )
    return parser.parse_args()


def parse_timeframe(value: str) -> tuple[int, int]:
    value = value.strip().lower()
    if len(value) < 2:
        raise ValueError("timeframe must look like 30m, 12h, or 7d")

    amount_text = value[:-1]
    unit = value[-1]
    if not amount_text.isdigit() or unit not in {"m", "h", "d"}:
        raise ValueError("timeframe must look like 30m, 12h, or 7d")

    amount = int(amount_text)
    if amount <= 0:
        raise ValueError("timeframe must be greater than zero")

    multipliers = {
        "m": 60,
        "h": 60 * 60,
        "d": 24 * 60 * 60,
    }
    now_ms = int(time.time() * 1000)
    from_ms = now_ms - amount * multipliers[unit] * 1000
    return from_ms, now_ms


def source_timeframe_labels(source: str, override: str | None) -> tuple[str, ...]:
    if override:
        return (override,)
    return AUTO_TIMEFRAMES[source]


def fetch_with_timeframe_fallback(
    source: str,
    override: str | None,
    fetcher: Any,
) -> tuple[dict[str, Any], int, int, str]:
    errors: list[str] = []

    for label in source_timeframe_labels(source, override):
        from_ms, to_ms = parse_timeframe(label)
        try:
            return fetcher(from_ms, to_ms), from_ms, to_ms, label
        except CloudflareAPIError as error:
            message = str(error).lower()
            if override or any(hint in message for hint in NON_TIMEFRAME_ERROR_HINTS):
                raise
            errors.append(f"{label}: {error}")

    raise CloudflareAPIError(
        f"{source.replace('_', ' ')} failed for every automatic timeframe. "
        + " | ".join(errors)
    )


def cloudflare_error_message(status: int | None, decoded: dict[str, Any] | None, body: str) -> str:
    errors = decoded.get("errors") if decoded else None
    messages = []
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                code = item.get("code")
                message = item.get("message", item)
                messages.append(f"{code}: {message}" if code is not None else str(message))
            else:
                messages.append(str(item))

    detail = "; ".join(messages) if messages else body
    message = f"Cloudflare API returned HTTP {status}: {detail}" if status else detail

    if status == 403 and decoded and any(
        isinstance(item, dict) and item.get("code") == 10000 for item in errors or []
    ):
        message += (
            "\n\n"
            "Authentication reached Cloudflare but was rejected for this request. Check:\n"
            "- CLOUDFLARE_API_TOKEN is the API token secret, not a token ID, dashboard cookie, or Global API Key.\n"
            "- The token is active: ./stats.py --verify-token\n"
            "- The token can access this account ID.\n"
            "- The token has Account > Workers Observability > Write permission.\n"
            "- For Web Analytics GraphQL, the token has Account > Account Analytics > Read permission.\n"
            "- For cache analytics, the token has Zone > Analytics > Read permission and access to the zone.\n"
            "- For zone listing, the token can read zones on this account.\n"
            "- For --list-sites, the token can read Web Analytics/RUM site info for this account.\n"
            "- Any token IP allowlist includes your current network."
        )

    return message


def request_json(
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{API_BASE}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            decoded = json.loads(body) if body else None
        except json.JSONDecodeError:
            decoded = None
        code = None
        if isinstance(decoded, dict) and isinstance(decoded.get("errors"), list) and decoded["errors"]:
            first_error = decoded["errors"][0]
            if isinstance(first_error, dict) and isinstance(first_error.get("code"), int):
                code = first_error["code"]
        raise CloudflareAPIError(
            cloudflare_error_message(error.code, decoded, body),
            status=error.code,
            code=code,
        ) from error
    except urllib.error.URLError as error:
        raise CloudflareAPIError(f"Could not reach Cloudflare API: {error.reason}") from error

    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as error:
        raise CloudflareAPIError(f"Cloudflare API returned invalid JSON: {body}") from error

    if not decoded.get("success", False):
        raise CloudflareAPIError(cloudflare_error_message(None, decoded, body))

    return decoded


def post_json(account_id: str, token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    return request_json(token, "POST", f"/accounts/{account_id}{path}", payload)


def verify_token(token: str) -> dict[str, Any]:
    return request_json(token, "GET", "/user/tokens/verify")


def graphql_json(token: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{API_BASE}/graphql"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            decoded = json.loads(body) if body else None
        except json.JSONDecodeError:
            decoded = None
        raise CloudflareAPIError(cloudflare_error_message(error.code, decoded, body)) from error
    except urllib.error.URLError as error:
        raise CloudflareAPIError(f"Could not reach Cloudflare GraphQL API: {error.reason}") from error

    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as error:
        raise CloudflareAPIError(f"Cloudflare GraphQL API returned invalid JSON: {body}") from error

    errors = decoded.get("errors")
    if errors:
        messages = []
        for item in errors:
            if isinstance(item, dict):
                messages.append(str(item.get("message", item)))
            else:
                messages.append(str(item))
        raise CloudflareAPIError(
            "Cloudflare GraphQL API returned errors: "
            + "; ".join(messages)
            + "\n\nCheck that the token has the analytics permission for this dataset. "
            "Web Analytics uses Account > Account Analytics > Read; "
            "zone HTTP/cache analytics uses Zone > Analytics > Read."
        )

    return decoded


def list_web_analytics_sites(account_id: str, token: str) -> dict[str, Any]:
    return request_json(token, "GET", f"/accounts/{account_id}/rum/site_info/list")


def list_zones(account_id: str, token: str, per_page: int = 50) -> list[ZoneInfo]:
    zones: list[ZoneInfo] = []
    page = 1

    while True:
        query = urllib.parse.urlencode(
            {
                "account.id": account_id,
                "per_page": per_page,
                "page": page,
                "order": "name",
                "direction": "asc",
            }
        )
        response = request_json(token, "GET", f"/zones?{query}")
        result = response.get("result")
        if not isinstance(result, list):
            break

        for zone in result:
            if not isinstance(zone, dict):
                continue
            plan = zone.get("plan")
            zones.append(
                ZoneInfo(
                    zone_id=str(zone.get("id", "")),
                    name=str(zone.get("name", "")),
                    status=str(zone.get("status", "")),
                    plan=str(plan.get("name", "")) if isinstance(plan, dict) else "",
                )
            )

        result_info = response.get("result_info")
        if not isinstance(result_info, dict):
            break
        total_pages = result_info.get("total_pages")
        if not isinstance(total_pages, int) or page >= total_pages:
            break
        page += 1

    return zones


def fetch_top_referers(
    account_id: str,
    token: str,
    from_ms: int,
    to_ms: int,
    dataset: str,
    limit: int,
) -> dict[str, Any]:
    payload = {
        "queryId": "top-referers",
        "view": "calculations",
        "limit": limit,
        "ignoreSeries": True,
        "timeframe": {"from": from_ms, "to": to_ms},
        "parameters": {
            "datasets": [dataset],
            "filterCombination": "and",
            "filters": [
                {
                    "key": REFERER_KEY,
                    "operation": "neq",
                    "type": "string",
                    "value": "",
                }
            ],
            "calculations": [{"operator": "count", "alias": "count"}],
            "groupBys": [{"type": "string", "value": REFERER_KEY}],
            "orderBy": {"value": "count", "order": "desc"},
            "limit": limit,
        },
    }
    return post_json(account_id, token, "/workers/observability/telemetry/query", payload)


def fetch_referer_values(
    account_id: str,
    token: str,
    from_ms: int,
    to_ms: int,
    dataset: str,
    limit: int,
) -> dict[str, Any]:
    payload = {
        "timeframe": {"from": from_ms, "to": to_ms},
        "key": REFERER_KEY,
        "type": "string",
        "datasets": [dataset],
        "filters": [
            {
                "key": REFERER_KEY,
                "operation": "neq",
                "type": "string",
                "value": "",
            }
        ],
        "limit": limit,
    }
    return post_json(account_id, token, "/workers/observability/telemetry/values", payload)


def build_web_analytics_filter(
    from_ms: int,
    to_ms: int,
    site_tags: list[str],
    host: str | None,
    include_bots: bool,
) -> dict[str, Any]:
    conditions: list[dict[str, Any]] = [
        {
            "datetime_geq": format_graphql_time(from_ms),
            "datetime_leq": format_graphql_time(to_ms),
        }
    ]

    if site_tags:
        if len(site_tags) == 1:
            conditions.append({"siteTag": site_tags[0]})
        else:
            conditions.append({"OR": [{"siteTag": site_tag} for site_tag in site_tags]})

    if host:
        conditions.append({"requestHost": host})

    if not include_bots:
        conditions.append({"bot": 0})

    return {"AND": conditions} if len(conditions) > 1 else conditions[0]


def fetch_web_analytics_referers(
    account_id: str,
    token: str,
    from_ms: int,
    to_ms: int,
    site_tags: list[str],
    host: str | None,
    include_bots: bool,
    limit: int,
) -> dict[str, Any]:
    query = f"""
query WebAnalyticsTopReferers(
  $accountTag: string!
  $filter: AccountRumPageloadEventsAdaptiveGroupsFilter_InputObject!
) {{
  viewer {{
    accounts(filter: {{ accountTag: $accountTag }}) {{
      topReferers: rumPageloadEventsAdaptiveGroups(
        filter: $filter
        limit: {limit}
        orderBy: [count_DESC]
      ) {{
        count
        sum {{
          visits
        }}
        dimensions {{
          refererHost
          refererPath
        }}
      }}
    }}
  }}
}}
""".strip()
    payload = {
        "query": query,
        "variables": {
            "accountTag": account_id,
            "filter": build_web_analytics_filter(from_ms, to_ms, site_tags, host, include_bots),
        },
    }
    return graphql_json(token, payload)


def build_http_filter(
    from_ms: int,
    to_ms: int,
    host: str | None = None,
    cache_statuses: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "datetime_geq": format_graphql_time(from_ms),
        "datetime_leq": format_graphql_time(to_ms),
    }
    if host:
        result["clientRequestHTTPHost"] = host
    if cache_statuses:
        result["cacheStatus_in"] = list(cache_statuses)
    return result


def fetch_zone_traffic(
    zone_id: str,
    token: str,
    from_ms: int,
    to_ms: int,
) -> dict[str, Any]:
    query = """
query ZoneTraffic(
  $zoneTag: string!
  $filter: ZoneHttpRequestsAdaptiveGroupsFilter_InputObject!
) {
  viewer {
    zones(filter: { zoneTag: $zoneTag }) {
      totals: httpRequestsAdaptiveGroups(
        filter: $filter
        limit: 1
      ) {
        count
        sum {
          edgeResponseBytes
          visits
        }
      }
    }
  }
}
""".strip()
    payload = {
        "query": query,
        "variables": {
            "zoneTag": zone_id,
            "filter": build_http_filter(from_ms, to_ms),
        },
    }
    return graphql_json(token, payload)


def fetch_cache_paths(
    zone_id: str,
    token: str,
    from_ms: int,
    to_ms: int,
    host: str | None,
    limit: int,
    query_limit: int,
    statuses: list[str],
) -> dict[str, Any]:
    query = f"""
query CachePaths(
  $zoneTag: string!
  $filterTotal: ZoneHttpRequestsAdaptiveGroupsFilter_InputObject!
  $filterCached: ZoneHttpRequestsAdaptiveGroupsFilter_InputObject!
  $filterOrigin: ZoneHttpRequestsAdaptiveGroupsFilter_InputObject!
  $filterStatuses: ZoneHttpRequestsAdaptiveGroupsFilter_InputObject!
) {{
  viewer {{
    zones(filter: {{ zoneTag: $zoneTag }}) {{
      total: httpRequestsAdaptiveGroups(
        filter: $filterTotal
        limit: {query_limit}
        orderBy: [count_DESC]
      ) {{
        count
        sum {{
          edgeResponseBytes
        }}
        dimensions {{
          clientRequestHTTPHost
          clientRequestPath
        }}
      }}
      cached: httpRequestsAdaptiveGroups(
        filter: $filterCached
        limit: {query_limit}
        orderBy: [count_DESC]
      ) {{
        count
        sum {{
          edgeResponseBytes
        }}
        dimensions {{
          clientRequestHTTPHost
          clientRequestPath
        }}
      }}
      origin: httpRequestsAdaptiveGroups(
        filter: $filterOrigin
        limit: {query_limit}
        orderBy: [count_DESC]
      ) {{
        count
        sum {{
          edgeResponseBytes
        }}
        dimensions {{
          clientRequestHTTPHost
          clientRequestPath
        }}
      }}
      statuses: httpRequestsAdaptiveGroups(
        filter: $filterStatuses
        limit: {limit}
        orderBy: [count_DESC]
      ) {{
        count
        sum {{
          edgeResponseBytes
        }}
        dimensions {{
          cacheStatus
          clientRequestHTTPHost
          clientRequestPath
          edgeResponseStatus
        }}
      }}
    }}
  }}
}}
""".strip()
    status_filter = statuses if statuses else [*CACHE_SERVED_STATUSES, *ORIGIN_STATUSES]
    payload = {
        "query": query,
        "variables": {
            "zoneTag": zone_id,
            "filterTotal": build_http_filter(from_ms, to_ms, host),
            "filterCached": build_http_filter(from_ms, to_ms, host, CACHE_SERVED_STATUSES),
            "filterOrigin": build_http_filter(from_ms, to_ms, host, ORIGIN_STATUSES),
            "filterStatuses": build_http_filter(from_ms, to_ms, host, status_filter),
        },
    }
    return graphql_json(token, payload)


def flatten_items(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from flatten_items(child)
    elif isinstance(value, list):
        for child in value:
            yield from flatten_items(child)


def pick_referer(row: dict[str, Any]) -> str | None:
    for key in (REFERER_KEY, "referer", "value"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value

    dimensions = row.get("dimensions")
    if isinstance(dimensions, dict):
        value = dimensions.get(REFERER_KEY) or dimensions.get("referer")
        if isinstance(value, str) and value:
            return value

    group = row.get("group")
    if isinstance(group, dict):
        value = group.get(REFERER_KEY) or group.get("referer")
        if isinstance(value, str) and value:
            return value

    groups = row.get("groups")
    if isinstance(groups, list):
        for item in groups:
            if isinstance(item, dict):
                value = item.get("value")
                if item.get("key") == REFERER_KEY and isinstance(value, str) and value:
                    return value

    return None


def pick_count(row: dict[str, Any]) -> int | float | None:
    for key in ("count", "_count", "COUNT", "value"):
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value

    aggregates = row.get("aggregates")
    if isinstance(aggregates, dict):
        for key in ("count", "_count", "COUNT"):
            value = aggregates.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value

    calculations = row.get("calculations")
    if isinstance(calculations, dict):
        for key in ("count", "_count", "COUNT"):
            value = calculations.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value

    return None


def extract_top_referers(response: dict[str, Any]) -> list[RefererCount]:
    rows: list[RefererCount] = []
    seen: set[tuple[str, int | float]] = set()

    for item in flatten_items(response.get("result")):
        referer = pick_referer(item)
        count = pick_count(item)
        if referer is None or count is None:
            continue
        key = (referer, count)
        if key in seen:
            continue
        rows.append(RefererCount(referer=referer, count=count))
        seen.add(key)

    rows.sort(key=lambda row: row.count, reverse=True)
    return rows


def extract_values(response: dict[str, Any]) -> list[str]:
    result = response.get("result")
    if not isinstance(result, list):
        return []

    values: list[str] = []
    for item in result:
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            values.append(item["value"])
    return values


def extract_web_analytics_referers(response: dict[str, Any]) -> list[WebAnalyticsRefererCount]:
    accounts = (
        response.get("data", {})
        .get("viewer", {})
        .get("accounts", [])
    )
    if not isinstance(accounts, list):
        return []

    rows: list[WebAnalyticsRefererCount] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        top_referers = account.get("topReferers", [])
        if not isinstance(top_referers, list):
            continue
        for row in top_referers:
            if not isinstance(row, dict):
                continue
            dimensions = row.get("dimensions")
            if not isinstance(dimensions, dict):
                continue

            host = dimensions.get("refererHost")
            path = dimensions.get("refererPath")
            if isinstance(host, str) and host:
                referer = host
                if isinstance(path, str) and path and path != "/":
                    referer += path
            else:
                referer = "(direct / none)"

            pageviews = row.get("count", 0)
            visits = row.get("sum", {}).get("visits", 0) if isinstance(row.get("sum"), dict) else 0
            if not isinstance(pageviews, (int, float)) or isinstance(pageviews, bool):
                pageviews = 0
            if not isinstance(visits, (int, float)) or isinstance(visits, bool):
                visits = 0

            rows.append(WebAnalyticsRefererCount(referer=referer, pageviews=pageviews, visits=visits))

    rows.sort(key=lambda row: row.pageviews, reverse=True)
    return rows


def first_zone(response: dict[str, Any]) -> dict[str, Any] | None:
    zones = response.get("data", {}).get("viewer", {}).get("zones", [])
    if isinstance(zones, list) and zones and isinstance(zones[0], dict):
        return zones[0]
    return None


def row_identity(row: dict[str, Any]) -> tuple[str, str] | None:
    dimensions = row.get("dimensions")
    if not isinstance(dimensions, dict):
        return None
    host = dimensions.get("clientRequestHTTPHost")
    path = dimensions.get("clientRequestPath")
    return (
        str(host) if isinstance(host, str) else "",
        str(path) if isinstance(path, str) and path else "/",
    )


def count_from_row(row: dict[str, Any]) -> int | float:
    value = row.get("count", 0)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return 0


def bytes_from_row(row: dict[str, Any]) -> int | float:
    row_sum = row.get("sum")
    if isinstance(row_sum, dict):
        value = row_sum.get("edgeResponseBytes", 0)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return 0


def visits_from_row(row: dict[str, Any]) -> int | float:
    row_sum = row.get("sum")
    if isinstance(row_sum, dict):
        value = row_sum.get("visits", 0)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return 0


def extract_zone_traffic(response: dict[str, Any], zone: ZoneInfo) -> ZoneTraffic:
    zone_data = first_zone(response)
    totals = zone_data.get("totals", []) if zone_data else []
    row = totals[0] if isinstance(totals, list) and totals and isinstance(totals[0], dict) else {}
    return ZoneTraffic(
        zone_id=zone.zone_id,
        name=zone.name,
        requests=count_from_row(row),
        bytes_sent=bytes_from_row(row),
        visits=visits_from_row(row),
    )


def map_counts(rows: Any) -> dict[tuple[str, str], tuple[int | float, int | float]]:
    result: dict[tuple[str, str], tuple[int | float, int | float]] = {}
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict):
            continue
        identity = row_identity(row)
        if identity is None:
            continue
        count, bytes_sent = result.get(identity, (0, 0))
        result[identity] = (count + count_from_row(row), bytes_sent + bytes_from_row(row))
    return result


def extract_cache_paths(response: dict[str, Any]) -> list[CachePathStats]:
    zone = first_zone(response)
    if not zone:
        return []

    total = map_counts(zone.get("total"))
    cached = map_counts(zone.get("cached"))
    origin = map_counts(zone.get("origin"))
    rows: list[CachePathStats] = []

    for (host, path), (total_count, bytes_sent) in total.items():
        cached_count = cached.get((host, path), (0, 0))[0]
        origin_count = origin.get((host, path), (0, 0))[0]
        rows.append(
            CachePathStats(
                host=host,
                path=path,
                total=total_count,
                cached=cached_count,
                origin=origin_count,
                bytes_sent=bytes_sent,
            )
        )

    rows.sort(key=lambda row: row.total, reverse=True)
    return rows


def extract_cache_statuses(response: dict[str, Any]) -> list[CacheStatusStats]:
    zone = first_zone(response)
    if not zone:
        return []
    status_rows = zone.get("statuses")
    if not isinstance(status_rows, list):
        return []

    rows: list[CacheStatusStats] = []
    for row in status_rows:
        if not isinstance(row, dict):
            continue
        dimensions = row.get("dimensions")
        if not isinstance(dimensions, dict):
            continue
        response_status = dimensions.get("edgeResponseStatus")
        rows.append(
            CacheStatusStats(
                host=str(dimensions.get("clientRequestHTTPHost", "")),
                path=str(dimensions.get("clientRequestPath", "/") or "/"),
                status=str(dimensions.get("cacheStatus", "")),
                count=count_from_row(row),
                bytes_sent=bytes_from_row(row),
                response_status=response_status
                if isinstance(response_status, (int, float)) and not isinstance(response_status, bool)
                else None,
            )
        )

    rows.sort(key=lambda row: row.count, reverse=True)
    return rows


def format_ms(timestamp_ms: int) -> str:
    timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_graphql_time(timestamp_ms: int) -> str:
    timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def format_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}"


def format_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def format_bytes(value: int | float) -> str:
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    size = float(value)
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024


def print_table(
    rows: list[RefererCount],
    from_ms: int,
    to_ms: int,
    title: str = "Top referers",
) -> None:
    print(f"{title} from {format_ms(from_ms)} to {format_ms(to_ms)}")
    print()

    if not rows:
        print("No referers found.")
        return

    rank_width = max(1, len(str(len(rows))))
    formatted_counts = [format_number(row.count) for row in rows]
    count_width = max(len("count"), *(len(count) for count in formatted_counts))
    print(f"{'#'.rjust(rank_width)}  {'count'.rjust(count_width)}  referer")
    print(f"{'-' * rank_width}  {'-' * count_width}  {'-' * 40}")
    for index, (row, count) in enumerate(zip(rows, formatted_counts), start=1):
        print(f"{str(index).rjust(rank_width)}  {str(count).rjust(count_width)}  {row.referer}")


def print_zones(zones: list[ZoneInfo]) -> None:
    if not zones:
        print("No zones found.")
        return

    print("Zones")
    print()
    print("zone_id                           name                          status      plan")
    print("--------------------------------  ----------------------------  ----------  ----------------")
    for zone in zones:
        print(
            f"{zone.zone_id.ljust(32)}  "
            f"{zone.name[:28].ljust(28)}  "
            f"{zone.status[:10].ljust(10)}  "
            f"{zone.plan}"
        )


def print_zones_with_data(rows: list[ZoneTraffic], from_ms: int, to_ms: int) -> None:
    print(f"Zones with HTTP data from {format_ms(from_ms)} to {format_ms(to_ms)}")
    print()

    rows = [row for row in rows if row.requests > 0]
    if not rows:
        print("No zones with HTTP request data found.")
        return

    rank_width = max(1, len(str(len(rows))))
    requests = [format_number(row.requests) for row in rows]
    visits = [format_number(row.visits) for row in rows]
    bytes_sent = [format_bytes(row.bytes_sent) for row in rows]
    requests_width = max(len("requests"), *(len(value) for value in requests))
    visits_width = max(len("visits"), *(len(value) for value in visits))
    bytes_width = max(len("bytes"), *(len(value) for value in bytes_sent))
    print(
        f"{'#'.rjust(rank_width)}  "
        f"{'requests'.rjust(requests_width)}  "
        f"{'visits'.rjust(visits_width)}  "
        f"{'bytes'.rjust(bytes_width)}  "
        "zone_id                           name"
    )
    print(
        f"{'-' * rank_width}  "
        f"{'-' * requests_width}  "
        f"{'-' * visits_width}  "
        f"{'-' * bytes_width}  "
        f"{'-' * 32}  {'-' * 28}"
    )
    for index, (row, request_count, visit_count, byte_count) in enumerate(
        zip(rows, requests, visits, bytes_sent),
        start=1,
    ):
        print(
            f"{str(index).rjust(rank_width)}  "
            f"{request_count.rjust(requests_width)}  "
            f"{visit_count.rjust(visits_width)}  "
            f"{byte_count.rjust(bytes_width)}  "
            f"{row.zone_id.ljust(32)}  "
            f"{row.name}"
        )


def print_web_analytics_table(
    rows: list[WebAnalyticsRefererCount],
    from_ms: int,
    to_ms: int,
) -> None:
    print(f"Web Analytics referers from {format_ms(from_ms)} to {format_ms(to_ms)}")
    print()

    if not rows:
        print("No Web Analytics referers found.")
        return

    rank_width = max(1, len(str(len(rows))))
    pageviews = [format_number(row.pageviews) for row in rows]
    visits = [format_number(row.visits) for row in rows]
    pageviews_width = max(len("pageviews"), *(len(value) for value in pageviews))
    visits_width = max(len("visits"), *(len(value) for value in visits))
    print(
        f"{'#'.rjust(rank_width)}  "
        f"{'pageviews'.rjust(pageviews_width)}  "
        f"{'visits'.rjust(visits_width)}  "
        "referer"
    )
    print(
        f"{'-' * rank_width}  "
        f"{'-' * pageviews_width}  "
        f"{'-' * visits_width}  "
        f"{'-' * 40}"
    )
    for index, (row, pageview_count, visit_count) in enumerate(
        zip(rows, pageviews, visits),
        start=1,
    ):
        print(
            f"{str(index).rjust(rank_width)}  "
            f"{pageview_count.rjust(pageviews_width)}  "
            f"{visit_count.rjust(visits_width)}  "
            f"{row.referer}"
        )


def print_cache_table(
    rows: list[CachePathStats],
    from_ms: int,
    to_ms: int,
    mode: str,
    limit: int,
) -> None:
    if mode == "fully-cached":
        rows = [row for row in rows if row.total > 0 and row.cached == row.total]
        title = "Fully cache-served URLs"
    elif mode == "cached":
        rows = sorted(rows, key=lambda row: row.cached, reverse=True)
        rows = [row for row in rows if row.cached > 0]
        title = "Top cache-served URLs"
    elif mode == "origin":
        rows = sorted(rows, key=lambda row: row.origin, reverse=True)
        rows = [row for row in rows if row.origin > 0]
        title = "Top origin-pressure URLs"
    else:
        rows = sorted(rows, key=lambda row: row.total, reverse=True)
        title = "Cache hit ratio by URL"

    rows = rows[:limit]
    print(f"{title} from {format_ms(from_ms)} to {format_ms(to_ms)}")
    print()

    if not rows:
        print("No cache URL stats found.")
        return

    rank_width = max(1, len(str(len(rows))))
    total = [format_number(row.total) for row in rows]
    cached = [format_number(row.cached) for row in rows]
    origin = [format_number(row.origin) for row in rows]
    ratios = [format_percent(row.hit_ratio) for row in rows]
    bytes_sent = [format_bytes(row.bytes_sent) for row in rows]
    total_width = max(len("total"), *(len(value) for value in total))
    cached_width = max(len("cached"), *(len(value) for value in cached))
    origin_width = max(len("origin"), *(len(value) for value in origin))
    ratio_width = max(len("cache%"), *(len(value) for value in ratios))
    bytes_width = max(len("bytes"), *(len(value) for value in bytes_sent))

    print(
        f"{'#'.rjust(rank_width)}  "
        f"{'total'.rjust(total_width)}  "
        f"{'cached'.rjust(cached_width)}  "
        f"{'origin'.rjust(origin_width)}  "
        f"{'cache%'.rjust(ratio_width)}  "
        f"{'bytes'.rjust(bytes_width)}  "
        "url"
    )
    print(
        f"{'-' * rank_width}  "
        f"{'-' * total_width}  "
        f"{'-' * cached_width}  "
        f"{'-' * origin_width}  "
        f"{'-' * ratio_width}  "
        f"{'-' * bytes_width}  "
        f"{'-' * 40}"
    )
    for index, (row, total_count, cached_count, origin_count, ratio, byte_count) in enumerate(
        zip(rows, total, cached, origin, ratios, bytes_sent),
        start=1,
    ):
        print(
            f"{str(index).rjust(rank_width)}  "
            f"{total_count.rjust(total_width)}  "
            f"{cached_count.rjust(cached_width)}  "
            f"{origin_count.rjust(origin_width)}  "
            f"{ratio.rjust(ratio_width)}  "
            f"{byte_count.rjust(bytes_width)}  "
            f"{row.url}"
        )


def print_cache_status_table(
    rows: list[CacheStatusStats],
    from_ms: int,
    to_ms: int,
) -> None:
    print(f"Cache statuses by URL from {format_ms(from_ms)} to {format_ms(to_ms)}")
    print()

    if not rows:
        print("No cache status stats found.")
        return

    rank_width = max(1, len(str(len(rows))))
    counts = [format_number(row.count) for row in rows]
    bytes_sent = [format_bytes(row.bytes_sent) for row in rows]
    count_width = max(len("count"), *(len(value) for value in counts))
    bytes_width = max(len("bytes"), *(len(value) for value in bytes_sent))
    print(
        f"{'#'.rjust(rank_width)}  "
        f"{'count'.rjust(count_width)}  "
        f"{'bytes'.rjust(bytes_width)}  "
        "cache_status  http  url"
    )
    print(
        f"{'-' * rank_width}  "
        f"{'-' * count_width}  "
        f"{'-' * bytes_width}  "
        f"{'-' * 12}  {'-' * 4}  {'-' * 40}"
    )
    for index, (row, count, byte_count) in enumerate(zip(rows, counts, bytes_sent), start=1):
        http_status = "" if row.response_status is None else format_number(row.response_status)
        print(
            f"{str(index).rjust(rank_width)}  "
            f"{count.rjust(count_width)}  "
            f"{byte_count.rjust(bytes_width)}  "
            f"{row.status[:12].ljust(12)}  "
            f"{http_status[:4].ljust(4)}  "
            f"{row.url}"
        )


def print_values(values: list[str], from_ms: int, to_ms: int) -> None:
    print(f"Referer values from {format_ms(from_ms)} to {format_ms(to_ms)}")
    print()

    if not values:
        print("No referer values found.")
        return

    for value in values:
        print(value)


def site_tags_from_args(args: argparse.Namespace) -> list[str]:
    tags = list(args.site_tag)
    env_value = os.environ.get("CLOUDFLARE_SITE_TAG")
    if env_value:
        tags.extend(env_value.split(","))

    result: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        clean = tag.strip()
        if clean and clean not in seen:
            result.append(clean)
            seen.add(clean)
    return result


def print_sites(response: dict[str, Any]) -> None:
    sites = response.get("result")
    if not isinstance(sites, list):
        print("No Web Analytics sites found.")
        return

    if not sites:
        print("No Web Analytics sites found.")
        return

    print("Web Analytics sites")
    print()
    print("site_tag                          host                  zone")
    print("--------------------------------  --------------------  --------------------")
    for site in sites:
        if not isinstance(site, dict):
            continue
        rules = site.get("rules")
        host = ""
        if isinstance(rules, list) and rules and isinstance(rules[0], dict):
            host = str(rules[0].get("host", ""))
        ruleset = site.get("ruleset")
        zone = str(ruleset.get("zone_name", "")) if isinstance(ruleset, dict) else ""
        print(f"{str(site.get('site_tag', '')).ljust(32)}  {host.ljust(20)}  {zone}")


def scan_zones_with_data(
    account_id: str,
    token: str,
    from_ms: int,
    to_ms: int,
    scan_limit: int,
) -> tuple[list[ZoneTraffic], list[ZoneScanError]]:
    zones = list_zones(account_id, token)
    rows: list[ZoneTraffic] = []
    errors: list[ZoneScanError] = []
    for zone in zones[:scan_limit]:
        if not zone.zone_id:
            continue
        try:
            response = fetch_zone_traffic(zone.zone_id, token, from_ms, to_ms)
        except CloudflareAPIError as error:
            errors.append(ZoneScanError(zone_id=zone.zone_id, name=zone.name, message=str(error)))
            continue
        rows.append(extract_zone_traffic(response, zone))

    rows.sort(key=lambda row: row.requests, reverse=True)
    return rows, errors


def main() -> int:
    args = parse_args()
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token:
        print("Missing CLOUDFLARE_API_TOKEN environment variable.", file=sys.stderr)
        return 2

    if args.verify_token:
        try:
            response = verify_token(token)
        except CloudflareAPIError as error:
            print(error, file=sys.stderr)
            return 1
        print(json.dumps(response, indent=2, sort_keys=True))
        return 0

    if not args.account_id:
        print(
            "Missing account ID. Pass --account-id or set CLOUDFLARE_ACCOUNT_ID.",
            file=sys.stderr,
        )
        return 2

    if args.limit <= 0:
        print("--limit must be greater than zero.", file=sys.stderr)
        return 2

    if args.zone_scan_limit <= 0:
        print("--zone-scan-limit must be greater than zero.", file=sys.stderr)
        return 2

    if args.cache_query_limit < 0:
        print("--cache-query-limit must be zero or greater.", file=sys.stderr)
        return 2

    if args.values and args.source != "workers":
        print("--values is only supported with --source workers.", file=sys.stderr)
        return 2

    if args.list_zones:
        try:
            zones = list_zones(args.account_id, token)
        except CloudflareAPIError as error:
            print(error, file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps([zone.__dict__ for zone in zones], indent=2, sort_keys=True))
        else:
            print_zones(zones)
        return 0

    if args.list_sites:
        try:
            response = list_web_analytics_sites(args.account_id, token)
        except CloudflareAPIError as error:
            print(error, file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(response, indent=2, sort_keys=True))
        else:
            print_sites(response)
        return 0

    if args.list_zones_with_data:
        scan_timeframe = args.timeframe or AUTO_TIMEFRAMES["cache"][0]
        try:
            from_ms, to_ms = parse_timeframe(scan_timeframe)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
        try:
            rows, scan_errors = scan_zones_with_data(
                args.account_id,
                token,
                from_ms,
                to_ms,
                args.zone_scan_limit,
            )
        except CloudflareAPIError as error:
            print(error, file=sys.stderr)
            return 1
        rows = rows[: args.limit]
        if args.json:
            print(
                json.dumps(
                    {
                        "zones": [row.__dict__ for row in rows],
                        "errors": [error.__dict__ for error in scan_errors],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print_zones_with_data(rows, from_ms, to_ms)
            if scan_errors:
                print(
                    f"\nSkipped {len(scan_errors)} zone analytics request(s). "
                    "Most common causes are missing Zone > Analytics > Read "
                    "or token zone-resource access.",
                    file=sys.stderr,
                )
                for error in scan_errors[:5]:
                    print(f"- {error.name} ({error.zone_id}): {error.message}", file=sys.stderr)
        return 0

    responses: dict[str, Any] = {}
    errors: dict[str, str] = {}
    timeframes: dict[str, tuple[int, int, str]] = {}

    wants_workers = args.source in {"workers", "both", "all"}
    wants_web_analytics = args.source in {"web-analytics", "both", "all"} and not args.values
    wants_cache = args.source in {"cache", "all"} and not args.values

    if wants_workers:
        try:
            if args.values:
                response, from_ms, to_ms, label = fetch_with_timeframe_fallback(
                    "workers",
                    args.timeframe,
                    lambda start, end: fetch_referer_values(
                        args.account_id,
                        token,
                        start,
                        end,
                        args.dataset,
                        args.limit,
                    ),
                )
            else:
                response, from_ms, to_ms, label = fetch_with_timeframe_fallback(
                    "workers",
                    args.timeframe,
                    lambda start, end: fetch_top_referers(
                        args.account_id,
                        token,
                        start,
                        end,
                        args.dataset,
                        args.limit,
                    ),
                )
            responses["workers"] = response
            timeframes["workers"] = (from_ms, to_ms, label)
        except (ValueError, CloudflareAPIError) as error:
            errors["workers"] = str(error)

    if wants_web_analytics:
        try:
            response, from_ms, to_ms, label = fetch_with_timeframe_fallback(
                "web_analytics",
                args.timeframe,
                lambda start, end: fetch_web_analytics_referers(
                    args.account_id,
                    token,
                    start,
                    end,
                    site_tags_from_args(args),
                    args.host,
                    args.include_bots,
                    args.limit,
                ),
            )
            responses["web_analytics"] = response
            timeframes["web_analytics"] = (from_ms, to_ms, label)
        except (ValueError, CloudflareAPIError) as error:
            errors["web_analytics"] = str(error)

    if wants_cache:
        if not args.zone_id:
            errors["cache"] = (
                "Missing zone ID. Pass --zone-id, set CLOUDFLARE_ZONE_ID, "
                "or run --list-zones-with-data to find a zone with HTTP analytics."
            )
        else:
            query_limit = args.cache_query_limit or max(args.limit * 10, 100)
            try:
                response, from_ms, to_ms, label = fetch_with_timeframe_fallback(
                    "cache",
                    args.timeframe,
                    lambda start, end: fetch_cache_paths(
                        args.zone_id,
                        token,
                        start,
                        end,
                        args.host,
                        args.limit,
                        query_limit,
                        [status.lower() for status in args.cache_status],
                    ),
                )
                responses["cache"] = response
                timeframes["cache"] = (from_ms, to_ms, label)
            except (ValueError, CloudflareAPIError) as error:
                errors["cache"] = str(error)

    if args.json:
        print(
            json.dumps(
                {
                    "responses": responses,
                    "errors": errors,
                    "timeframes": {
                        source: {"from": values[0], "to": values[1], "label": values[2]}
                        for source, values in timeframes.items()
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        printed = False
        if "workers" in responses:
            from_ms, to_ms, _label = timeframes["workers"]
            if args.values:
                print_values(extract_values(responses["workers"]), from_ms, to_ms)
            else:
                print_table(
                    extract_top_referers(responses["workers"]),
                    from_ms,
                    to_ms,
                    "Workers Observability referers",
                )
            printed = True

        if "web_analytics" in responses:
            if printed:
                print()
            from_ms, to_ms, _label = timeframes["web_analytics"]
            print_web_analytics_table(
                extract_web_analytics_referers(responses["web_analytics"]),
                from_ms,
                to_ms,
            )
            printed = True

        if "cache" in responses:
            if printed:
                print()
            from_ms, to_ms, _label = timeframes["cache"]
            if args.cache_mode == "statuses":
                print_cache_status_table(
                    extract_cache_statuses(responses["cache"]),
                    from_ms,
                    to_ms,
                )
            else:
                print_cache_table(
                    extract_cache_paths(responses["cache"]),
                    from_ms,
                    to_ms,
                    args.cache_mode,
                    args.limit,
                )
            printed = True

        for source, message in errors.items():
            label = source.replace("_", " ")
            print(f"{label} failed: {message}", file=sys.stderr)

    if errors and not responses:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
