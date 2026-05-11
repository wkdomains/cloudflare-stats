#!/usr/bin/env python3
"""Query Cloudflare referer stats from Workers Observability and Web Analytics."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


API_BASE = "https://api.cloudflare.com/client/v4"
REFERER_KEY = "$workers.event.request.headers.referer"
DEFAULT_DATASET = "cloudflare-workers"


@dataclass(frozen=True)
class RefererCount:
    referer: str
    count: int | float


@dataclass(frozen=True)
class WebAnalyticsRefererCount:
    referer: str
    pageviews: int | float
    visits: int | float


class CloudflareAPIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show top Cloudflare referers from Workers Observability and Web Analytics.",
    )
    parser.add_argument(
        "--account-id",
        default=os.environ.get("CLOUDFLARE_ACCOUNT_ID"),
        help="Cloudflare account ID. Defaults to CLOUDFLARE_ACCOUNT_ID.",
    )
    parser.add_argument(
        "--timeframe",
        default="12h",
        help="Relative time window such as 30m, 12h, 7d. Defaults to 12h.",
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
        choices=("workers", "web-analytics", "both"),
        default="both",
        help="Data source to query. Defaults to both.",
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
            + "\n\nCheck that the token has Account > Account Analytics > Read permission."
        )

    return decoded


def list_web_analytics_sites(account_id: str, token: str) -> dict[str, Any]:
    return request_json(token, "GET", f"/accounts/{account_id}/rum/site_info/list")


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

    if args.values and args.source != "workers":
        print("--values is only supported with --source workers.", file=sys.stderr)
        return 2

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

    try:
        from_ms, to_ms = parse_timeframe(args.timeframe)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1

    responses: dict[str, Any] = {}
    errors: dict[str, str] = {}

    if args.source in {"workers", "both"}:
        try:
            if args.values:
                responses["workers"] = fetch_referer_values(
                    args.account_id,
                    token,
                    from_ms,
                    to_ms,
                    args.dataset,
                    args.limit,
                )
            else:
                responses["workers"] = fetch_top_referers(
                    args.account_id,
                    token,
                    from_ms,
                    to_ms,
                    args.dataset,
                    args.limit,
                )
        except CloudflareAPIError as error:
            errors["workers"] = str(error)

    if args.source in {"web-analytics", "both"} and not args.values:
        try:
            responses["web_analytics"] = fetch_web_analytics_referers(
                args.account_id,
                token,
                from_ms,
                to_ms,
                site_tags_from_args(args),
                args.host,
                args.include_bots,
                args.limit,
            )
        except CloudflareAPIError as error:
            errors["web_analytics"] = str(error)

    if args.json:
        print(json.dumps({"responses": responses, "errors": errors}, indent=2, sort_keys=True))
    else:
        printed = False
        if "workers" in responses:
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
            print_web_analytics_table(
                extract_web_analytics_referers(responses["web_analytics"]),
                from_ms,
                to_ms,
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
