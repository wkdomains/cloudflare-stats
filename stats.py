#!/usr/bin/env python3
"""Query Cloudflare Workers Observability referer stats."""

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


class CloudflareAPIError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show top Cloudflare Workers request referers from Workers Observability.",
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
        "--json",
        action="store_true",
        help="Print the raw Cloudflare response JSON.",
    )
    parser.add_argument(
        "--values",
        action="store_true",
        help="List unique referer values instead of top referers by count.",
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


def post_json(account_id: str, token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{API_BASE}/accounts/{account_id}{path}"
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
        raise CloudflareAPIError(f"Cloudflare API returned HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise CloudflareAPIError(f"Could not reach Cloudflare API: {error.reason}") from error

    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as error:
        raise CloudflareAPIError(f"Cloudflare API returned invalid JSON: {body}") from error

    if not decoded.get("success", False):
        errors = decoded.get("errors") or []
        messages = ", ".join(str(item.get("message", item)) for item in errors)
        raise CloudflareAPIError(messages or "Cloudflare API request failed")

    return decoded


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


def format_ms(timestamp_ms: int) -> str:
    timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}"


def print_table(rows: list[RefererCount], from_ms: int, to_ms: int) -> None:
    print(f"Top referers from {format_ms(from_ms)} to {format_ms(to_ms)}")
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


def print_values(values: list[str], from_ms: int, to_ms: int) -> None:
    print(f"Referer values from {format_ms(from_ms)} to {format_ms(to_ms)}")
    print()

    if not values:
        print("No referer values found.")
        return

    for value in values:
        print(value)


def main() -> int:
    args = parse_args()
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token:
        print("Missing CLOUDFLARE_API_TOKEN environment variable.", file=sys.stderr)
        return 2

    if not args.account_id:
        print(
            "Missing account ID. Pass --account-id or set CLOUDFLARE_ACCOUNT_ID.",
            file=sys.stderr,
        )
        return 2

    if args.limit <= 0:
        print("--limit must be greater than zero.", file=sys.stderr)
        return 2

    try:
        from_ms, to_ms = parse_timeframe(args.timeframe)
        if args.values:
            response = fetch_referer_values(
                args.account_id,
                token,
                from_ms,
                to_ms,
                args.dataset,
                args.limit,
            )
        else:
            response = fetch_top_referers(
                args.account_id,
                token,
                from_ms,
                to_ms,
                args.dataset,
                args.limit,
            )
    except (ValueError, CloudflareAPIError) as error:
        print(error, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(response, indent=2, sort_keys=True))
    elif args.values:
        print_values(extract_values(response), from_ms, to_ms)
    else:
        print_table(extract_top_referers(response), from_ms, to_ms)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
