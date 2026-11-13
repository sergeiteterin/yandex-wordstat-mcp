"""Thin async client for the Yandex Cloud Search API Wordstat methods."""

from __future__ import annotations

import calendar
import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

BASE_URL = "https://searchapi.api.cloud.yandex.net/v2/wordstat"
DEFAULT_TIMEOUT = 30.0

DEVICES = {
    "all": "DEVICE_ALL",
    "desktop": "DEVICE_DESKTOP",
    "phone": "DEVICE_PHONE",
    "tablet": "DEVICE_TABLET",
}
PERIODS = {
    "monthly": "PERIOD_MONTHLY",
    "weekly": "PERIOD_WEEKLY",
    "daily": "PERIOD_DAILY",
}
REGION_TYPES = {
    "all": "REGION_ALL",
    "regions": "REGION_REGIONS",
    "cities": "REGION_CITIES",
}


class WordstatError(ToolError):
    """API error or incomplete config. Subclassing ToolError makes MCP pass the
    message to the model instead of hiding it as an unexpected crash."""


@dataclass(frozen=True)
class Credentials:
    api_key: str | None
    iam_token: str | None
    folder_id: str

    @classmethod
    def from_env(cls) -> "Credentials":
        api_key = os.getenv("YANDEX_SEARCH_API_KEY") or None
        iam_token = os.getenv("YANDEX_IAM_TOKEN") or None
        folder_id = os.getenv("YANDEX_FOLDER_ID") or ""
        if not (api_key or iam_token):
            raise WordstatError(
                "Set YANDEX_SEARCH_API_KEY (or YANDEX_IAM_TOKEN) in the MCP server environment."
            )
        if not folder_id:
            raise WordstatError("Set YANDEX_FOLDER_ID in the MCP server environment.")
        return cls(api_key=api_key, iam_token=iam_token, folder_id=folder_id)

    def auth_header(self) -> str:
        if self.api_key:
            return f"Api-Key {self.api_key}"
        return f"Bearer {self.iam_token}"


def map_choice(value: str, choices: dict[str, str], field: str) -> str:
    key = value.strip().lower()
    if key not in choices:
        raise WordstatError(f"Unknown {field} '{value}'. Use one of: {', '.join(choices)}.")
    return choices[key]


def normalize_range(period: str, start: date, end: date | None) -> tuple[date, date | None]:
    """Snap dates to what the API accepts for each period.

    weekly: from = Monday, to = Sunday. monthly: from = 1st, to = last day of month.
    """
    if period == "PERIOD_WEEKLY":
        start = start - timedelta(days=start.weekday())
        if end is not None:
            end = end + timedelta(days=6 - end.weekday())
    elif period == "PERIOD_MONTHLY":
        start = start.replace(day=1)
        if end is not None:
            end = end.replace(day=calendar.monthrange(end.year, end.month)[1])
    if end is not None and end < start:
        raise WordstatError(f"to_date {end} is earlier than from_date {start}.")
    return start, end


def _ts(d: date) -> str:
    return f"{d.isoformat()}T00:00:00Z"


def _int(value: Any) -> Any:
    # The API encodes int64 counters as strings.
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


class WordstatClient:
    def __init__(self, credentials: Credentials, http: httpx.AsyncClient | None = None):
        self._creds = credentials
        self._http = http or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self._regions_tree: list[dict[str, Any]] | None = None
        self._region_names: dict[str, str] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        body = {**body, "folderId": self._creds.folder_id}
        try:
            resp = await self._http.post(
                f"{BASE_URL}/{method}",
                json=body,
                headers={"Authorization": self._creds.auth_header()},
            )
        except httpx.HTTPError as exc:
            raise WordstatError(f"Request to Wordstat API failed: {exc.__class__.__name__}") from exc
        if resp.status_code != 200:
            try:
                message = resp.json().get("message", resp.text)
            except ValueError:
                message = resp.text
            hint = ""
            if resp.status_code in (401, 403):
                hint = " Check the API key and that the service account has the search-api.editor role in this folder."
            elif resp.status_code == 400 and message == "Bad Request" and "fromDate" in body:
                hint = " Wordstat data lags by several weeks; try an earlier from_date / to_date."
            raise WordstatError(f"Wordstat API error {resp.status_code}: {message}.{hint}")
        return resp.json()

    # ---- regions -------------------------------------------------------

    async def regions_tree(self) -> list[dict[str, Any]]:
        if self._regions_tree is None:
            data = await self._post("getRegionsTree", {})
            self._regions_tree = data.get("regions", [])
        return self._regions_tree

    async def region_names(self) -> dict[str, str]:
        if self._region_names is None:
            names: dict[str, str] = {}

            def walk(nodes: list[dict[str, Any]]) -> None:
                for node in nodes:
                    names[str(node["id"])] = node.get("label", "")
                    walk(node.get("children", []))

            walk(await self.regions_tree())
            self._region_names = names
        return self._region_names

    async def resolve_regions(self, regions: list[str] | None) -> list[str] | None:
        """Accept region IDs or exact names ("Москва", "Санкт-Петербург")."""
        if not regions:
            return None
        names = await self.region_names()
        by_label = {label.casefold(): rid for rid, label in names.items()}
        resolved = []
        for item in regions:
            item = str(item).strip()
            if item.isdigit():
                resolved.append(item)
            elif item.casefold() in by_label:
                resolved.append(by_label[item.casefold()])
            else:
                raise WordstatError(
                    f"Unknown region '{item}'. Use find_region to look up the ID."
                )
        return resolved

    # ---- wordstat methods ---------------------------------------------

    async def top_requests(
        self,
        phrase: str,
        num_phrases: int,
        regions: list[str] | None,
        devices: list[str] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"phrase": phrase, "numPhrases": str(num_phrases)}
        if regions:
            body["regions"] = regions
        if devices:
            body["devices"] = devices
        data = await self._post("topRequests", body)
        return {
            "phrase": phrase,
            "total_count": _int(data.get("totalCount")),
            "results": [
                {"phrase": r["phrase"], "count": _int(r.get("count"))}
                for r in data.get("results", [])
            ],
            "associations": [
                {"phrase": r["phrase"], "count": _int(r.get("count"))}
                for r in data.get("associations", [])
            ],
        }

    async def dynamics(
        self,
        phrase: str,
        period: str,
        from_date: date,
        to_date: date | None,
        regions: list[str] | None,
        devices: list[str] | None,
    ) -> dict[str, Any]:
        from_date, to_date = normalize_range(period, from_date, to_date)
        body: dict[str, Any] = {"phrase": phrase, "period": period, "fromDate": _ts(from_date)}
        if to_date is not None:
            body["toDate"] = _ts(to_date)
        if regions:
            body["regions"] = regions
        if devices:
            body["devices"] = devices
        data = await self._post("dynamics", body)
        return {
            "phrase": phrase,
            "period": period.removeprefix("PERIOD_").lower(),
            "results": [
                {
                    "date": r["date"][:10],
                    "count": _int(r.get("count")),
                    "share_percent": round(r.get("share", 0.0), 6),
                }
                for r in data.get("results", [])
            ],
        }

    async def regions_distribution(
        self,
        phrase: str,
        region_type: str,
        devices: list[str] | None,
        limit: int,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"phrase": phrase, "region": region_type}
        if devices:
            body["devices"] = devices
        data = await self._post("regions", body)
        names = await self.region_names()
        rows = [
            {
                "region_id": str(r["region"]),
                "region": names.get(str(r["region"]), ""),
                "count": _int(r.get("count")),
                "share_percent": round(r.get("share", 0.0), 6),
                "affinity_index": round(r.get("affinityIndex", 0.0), 1),
            }
            for r in data.get("results", [])
        ]
        rows.sort(key=lambda r: r["count"] if isinstance(r["count"], int) else 0, reverse=True)
        return {
            "phrase": phrase,
            "region_type": region_type.removeprefix("REGION_").lower(),
            "total_regions": len(rows),
            "results": rows[:limit],
        }

    async def find_region(self, query: str, limit: int) -> list[dict[str, str]]:
        q = query.strip().casefold()
        matches: list[dict[str, str]] = []

        def walk(nodes: list[dict[str, Any]], path: list[str]) -> None:
            for node in nodes:
                label = node.get("label", "")
                if q in label.casefold():
                    matches.append(
                        {"id": str(node["id"]), "name": label, "path": " / ".join(path + [label])}
                    )
                walk(node.get("children", []), path + [label])

        walk(await self.regions_tree(), [])
        # Exact matches first, then shorter names.
        matches.sort(key=lambda m: (m["name"].casefold() != q, len(m["name"])))
        return matches[:limit]
