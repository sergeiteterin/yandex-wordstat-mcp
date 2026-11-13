"""MCP server exposing Yandex Wordstat statistics via the Yandex Cloud Search API."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import (
    DEVICES,
    PERIODS,
    REGION_TYPES,
    Credentials,
    WordstatClient,
    map_choice,
)

INSTRUCTIONS = """\
Yandex Wordstat: how often people search for a phrase in Yandex.
- top_requests: monthly volume for a phrase plus related queries (keyword research).
- frequencies: base and exact volume for up to 100 phrases in one call (compare wordings).
- dynamics: how volume changes over time (seasonality, trends).
- regions_distribution: where people search for a phrase, with an affinity index.
- find_region: look up region IDs by name for the `regions` filters.
Phrases support Wordstat operators: "quotes" fix the word set, !word fixes the form,
+word keeps stop words, -word excludes. Data lags real time by several weeks.
"""

logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = MCPServer(name="yandex-wordstat", instructions=INSTRUCTIONS)
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

Device = Literal["all", "desktop", "phone", "tablet"]
Phrase = Annotated[str, Field(min_length=1, max_length=400, description="Search phrase, Wordstat operators allowed.")]
Regions = Annotated[
    list[str] | None,
    Field(description='Region IDs or exact names, e.g. ["213"] or ["Москва"]. Omit for all regions.'),
]
Devices = Annotated[list[Device] | None, Field(description="Device filter. Omit for all devices.")]

_client: WordstatClient | None = None


def client() -> WordstatClient:
    global _client
    if _client is None:
        _client = WordstatClient(Credentials.from_env())
    return _client


def _devices(devices: list[str] | None) -> list[str] | None:
    return [map_choice(d, DEVICES, "device") for d in devices] if devices else None


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool(annotations=READ_ONLY)
async def top_requests(
    phrase: Phrase,
    num_phrases: Annotated[int, Field(ge=1, le=2000, description="How many related queries to return.")] = 50,
    regions: Regions = None,
    devices: Devices = None,
) -> str:
    """Search volume for a phrase over the last 30 days, plus the most popular queries
    containing it and similar ("associated") queries. Use for keyword research."""
    c = client()
    result = await c.top_requests(phrase, num_phrases, await c.resolve_regions(regions), _devices(devices))
    return _dump(result)


@mcp.tool(annotations=READ_ONLY)
async def frequencies(
    phrases: Annotated[
        list[Phrase],
        Field(min_length=1, max_length=100, description="Phrases to check, up to 7 words each."),
    ],
    regions: Regions = None,
    devices: Devices = None,
    mode: Annotated[
        Literal["both", "exact", "base"],
        Field(description="both = base and exact, or only one of them (half the API requests)."),
    ] = "both",
) -> str:
    """Base and exact search volume over the last 30 days for a list of phrases in one call.
    Use it to check real demand for specific wordings instead of calling top_requests per phrase.
    `base` counts every query containing the words, so it is inflated by longer queries.
    `exact` counts only queries made of exactly these words in these forms ("!word1 !word2") —
    trust it when deciding whether a phrase deserves its own page. `exact_share_percent` is
    exact / base. Each phrase costs up to 2 API requests (1 with mode "exact" or "base").
    A phrase that fails gets an `error` field; the others are still returned."""
    c = client()
    result = await c.frequencies(phrases, mode, await c.resolve_regions(regions), _devices(devices))
    return _dump(result)


@mcp.tool(annotations=READ_ONLY)
async def dynamics(
    phrase: Phrase,
    period: Annotated[Literal["monthly", "weekly", "daily"], Field(description="Aggregation step.")] = "monthly",
    from_date: Annotated[
        date | None,
        Field(
            description="Start date (YYYY-MM-DD). Default: ~12 months back for monthly, ~12 weeks "
            "for weekly, 55 days for daily. Daily data is only available for the last 60 days."
        ),
    ] = None,
    to_date: Annotated[date | None, Field(description="End date (YYYY-MM-DD). Default: latest available.")] = None,
    regions: Regions = None,
    devices: Devices = None,
) -> str:
    """How search volume for a phrase changes over time. `share_percent` is the phrase's
    share of all Yandex queries in that period. Dates are snapped automatically:
    weeks to Monday–Sunday, months to the 1st and the last day.
    Data lags by about a month; daily data covers only the last 60 days."""
    if from_date is None:
        # Data lags ~a month, and daily from_date must be within the last 60 days.
        back = {"monthly": 395, "weekly": 7 * 16, "daily": 55}[period]
        from_date = date.today() - timedelta(days=back)
    c = client()
    result = await c.dynamics(
        phrase,
        map_choice(period, PERIODS, "period"),
        from_date,
        to_date,
        await c.resolve_regions(regions),
        _devices(devices),
    )
    return _dump(result)


@mcp.tool(annotations=READ_ONLY)
async def regions_distribution(
    phrase: Phrase,
    region_type: Annotated[
        Literal["cities", "regions", "all"],
        Field(description="cities = cities only, regions = federal subjects, all = every level."),
    ] = "regions",
    devices: Devices = None,
    limit: Annotated[int, Field(ge=1, le=1000, description="Max rows, sorted by count.")] = 30,
) -> str:
    """Where people search for a phrase over the last 30 days. `affinity_index` > 100 means
    the region searches for it more than average, < 100 less."""
    result = await client().regions_distribution(
        phrase, map_choice(region_type, REGION_TYPES, "region_type"), _devices(devices), limit
    )
    return _dump(result)


@mcp.tool(annotations=READ_ONLY)
async def find_region(
    query: Annotated[str, Field(min_length=1, description="Part of a region name, e.g. 'Казань'.")],
    limit: Annotated[int, Field(ge=1, le=100)] = 10,
) -> str:
    """Find Wordstat region IDs by name. Returns id, name and the path in the region tree."""
    result = await client().find_region(query, limit)
    return _dump(result) if result else f"No regions match '{query}'."


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
