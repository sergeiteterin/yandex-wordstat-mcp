import json
from datetime import date

import httpx
import pytest
import respx

from yandex_wordstat_mcp.client import (
    BASE_URL,
    Credentials,
    WordstatClient,
    WordstatError,
    normalize_range,
)

CREDS = Credentials(api_key="test-key", iam_token=None, folder_id="b1gtest")

TREE = {
    "regions": [
        {
            "id": "225",
            "label": "Россия",
            "children": [
                {"id": "213", "label": "Москва"},
                {"id": "2", "label": "Санкт-Петербург"},
                {"id": "43", "label": "Казань"},
            ],
        }
    ]
}


@pytest.fixture
def api():
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.post("/getRegionsTree").respond(json=TREE)
        yield mock


@pytest.fixture
async def client():
    c = WordstatClient(CREDS)
    yield c
    await c.aclose()


# ---- normalize_range ---------------------------------------------------


def test_weekly_snaps_to_monday_and_sunday():
    start, end = normalize_range("PERIOD_WEEKLY", date(2026, 8, 12), date(2026, 9, 2))
    assert start == date(2026, 8, 10)  # Monday
    assert end == date(2026, 9, 6)  # Sunday


def test_monthly_snaps_to_month_bounds():
    start, end = normalize_range("PERIOD_MONTHLY", date(2024, 1, 15), date(2024, 2, 10))
    assert start == date(2024, 1, 1)
    assert end == date(2024, 2, 29)  # leap year


def test_daily_is_unchanged():
    assert normalize_range("PERIOD_DAILY", date(2026, 10, 3), None) == (date(2026, 10, 3), None)


def test_reversed_range_rejected():
    with pytest.raises(WordstatError):
        normalize_range("PERIOD_DAILY", date(2026, 10, 3), date(2026, 10, 1))


# ---- credentials -------------------------------------------------------


def test_credentials_require_key(monkeypatch):
    monkeypatch.delenv("YANDEX_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_IAM_TOKEN", raising=False)
    monkeypatch.setenv("YANDEX_FOLDER_ID", "b1g")
    with pytest.raises(WordstatError, match="YANDEX_SEARCH_API_KEY"):
        Credentials.from_env()


def test_iam_token_uses_bearer(monkeypatch):
    monkeypatch.delenv("YANDEX_SEARCH_API_KEY", raising=False)
    monkeypatch.setenv("YANDEX_IAM_TOKEN", "t1")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "b1g")
    assert Credentials.from_env().auth_header() == "Bearer t1"


# ---- API calls ---------------------------------------------------------


async def test_top_requests_sends_folder_and_parses_counts(api, client):
    route = api.post("/topRequests").respond(
        json={
            "totalCount": "100",
            "results": [{"phrase": "a", "count": "60"}],
            "associations": [{"phrase": "b", "count": "5"}],
        }
    )
    result = await client.top_requests("a", 10, ["213"], ["DEVICE_PHONE"])

    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "phrase": "a",
        "numPhrases": "10",
        "regions": ["213"],
        "devices": ["DEVICE_PHONE"],
        "folderId": "b1gtest",
    }
    assert route.calls.last.request.headers["Authorization"] == "Api-Key test-key"
    assert result["total_count"] == 100
    assert result["results"] == [{"phrase": "a", "count": 60}]


async def test_dynamics_sends_normalized_dates(api, client):
    route = api.post("/dynamics").respond(
        json={"results": [{"date": "2026-08-10T00:00:00Z", "count": "7", "share": 0.1}]}
    )
    result = await client.dynamics("a", "PERIOD_WEEKLY", date(2026, 8, 12), None, None, None)

    sent = json.loads(route.calls.last.request.content)
    assert sent["fromDate"] == "2026-08-10T00:00:00Z"
    assert "toDate" not in sent
    assert result["results"] == [{"date": "2026-08-10", "count": 7, "share_percent": 0.1}]


async def test_regions_distribution_adds_names_and_sorts(api, client):
    api.post("/regions").respond(
        json={
            "results": [
                {"region": "43", "count": "10", "share": 0.1, "affinityIndex": 120.04},
                {"region": "213", "count": "50", "share": 0.2, "affinityIndex": 90},
            ]
        }
    )
    result = await client.regions_distribution("a", "REGION_CITIES", None, 1)
    assert result["total_regions"] == 2
    assert result["results"] == [
        {
            "region_id": "213",
            "region": "Москва",
            "count": 50,
            "share_percent": 0.2,
            "affinity_index": 90,
        }
    ]


async def test_resolve_regions_by_name_and_id(api, client):
    assert await client.resolve_regions(["москва", "2"]) == ["213", "2"]
    with pytest.raises(WordstatError, match="find_region"):
        await client.resolve_regions(["Нарния"])


async def test_find_region_exact_match_first(api, client):
    found = await client.find_region("казань", 5)
    assert found == [{"id": "43", "name": "Казань", "path": "Россия / Казань"}]


async def test_api_error_includes_message_and_hint(api, client):
    api.post("/topRequests").respond(401, json={"code": 16, "message": "Unauthenticated"})
    with pytest.raises(WordstatError, match="401.*search-api.editor"):
        await client.top_requests("a", 1, None, None)


async def test_network_error_does_not_leak_key(api, client):
    api.post("/topRequests").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(WordstatError) as exc:
        await client.top_requests("a", 1, None, None)
    assert "test-key" not in str(exc.value)
