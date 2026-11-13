import json

import httpx
import pytest
import respx

from yandex_wordstat_mcp import client as client_module
from yandex_wordstat_mcp.client import (
    BASE_URL,
    Credentials,
    WordstatClient,
    WordstatError,
    exact_query,
)

CREDS = Credentials(api_key="test-key", iam_token=None, folder_id="b1gtest")

COUNTS = {
    "карьерный коуч": 821,
    '"!карьерный !коуч"': 37,
    "бизнес коуч": 500,
    '"!бизнес !коуч"': 50,
    "нет такого": 0,
    '"!нет !такого"': 0,
}


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(client_module, "RETRY_BASE_DELAY", 0)


@pytest.fixture
def api():
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def client():
    c = WordstatClient(CREDS)
    yield c
    await c.aclose()


def by_phrase(responses):
    """Answer topRequests depending on the phrase in the request body."""

    def handler(request):
        answer = responses[json.loads(request.content)["phrase"]]
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json={"totalCount": str(answer), "results": []})

    return handler


def sent_phrases(route):
    return sorted(json.loads(call.request.content)["phrase"] for call in route.calls)


# ---- exact query builder -----------------------------------------------


def test_exact_query_fixes_every_word():
    assert exact_query("карьерный  коуч") == '"!карьерный !коуч"'


def test_exact_query_rebuilds_existing_operators():
    assert exact_query('"!купить +в [москве] (квартиру|дом)" -аренда') == (
        '"!купить !в !москве !квартирудом"'
    )
    assert exact_query("санкт-петербург ! коуч icf 2026") == '"!санкт-петербург !коуч !icf !2026"'


def test_exact_query_rejects_long_and_empty_phrases():
    assert exact_query("а б в г д е ж").count("!") == 7
    with pytest.raises(WordstatError, match="8 words"):
        exact_query("а б в г д е ж з")
    with pytest.raises(WordstatError, match="no words"):
        exact_query('"!" -минус')


# ---- frequencies -------------------------------------------------------


async def test_frequencies_both_keeps_order_and_computes_share(api, client):
    route = api.post("/topRequests").mock(side_effect=by_phrase(COUNTS))
    result = await client.frequencies(
        ["бизнес коуч", "карьерный коуч", "нет такого"], "both", ["213"], ["DEVICE_PHONE"]
    )

    assert result["results"] == [
        {"phrase": "бизнес коуч", "base": 500, "exact": 50, "exact_share_percent": 10.0},
        {"phrase": "карьерный коуч", "base": 821, "exact": 37, "exact_share_percent": 4.5},
        {"phrase": "нет такого", "base": 0, "exact": 0, "exact_share_percent": None},
    ]
    assert "base" in result["note"] and "exact" in result["note"]
    assert json.loads(route.calls.last.request.content) | {"phrase": ""} == {
        "phrase": "",
        "numPhrases": "1",
        "regions": ["213"],
        "devices": ["DEVICE_PHONE"],
        "folderId": "b1gtest",
    }


async def test_frequencies_mode_exact_and_base(api, client):
    route = api.post("/topRequests").mock(side_effect=by_phrase(COUNTS))

    exact = await client.frequencies(["карьерный коуч"], "exact", None, None)
    assert exact["results"] == [{"phrase": "карьерный коуч", "exact": 37}]
    assert sent_phrases(route) == ['"!карьерный !коуч"']

    base = await client.frequencies(["карьерный коуч"], "base", None, None)
    assert base["results"] == [{"phrase": "карьерный коуч", "base": 821}]
    assert sent_phrases(route) == ['"!карьерный !коуч"', "карьерный коуч"]
    assert route.call_count == 2


async def test_frequencies_phrase_with_operators(api, client):
    route = api.post("/topRequests").mock(
        side_effect=by_phrase({"+как стать коучем": 300, '"!как !стать !коучем"': 30})
    )
    result = await client.frequencies(["+как стать коучем"], "both", None, None)
    assert result["results"][0]["base"] == 300
    assert result["results"][0]["exact"] == 30
    assert sent_phrases(route) == ['"!как !стать !коучем"', "+как стать коучем"]


async def test_frequencies_long_phrase_is_not_sent(api, client):
    route = api.post("/topRequests").mock(side_effect=by_phrase(COUNTS))
    result = await client.frequencies(["а б в г д е ж з", "карьерный коуч"], "both", None, None)

    assert result["results"][0]["phrase"] == "а б в г д е ж з"
    assert "at most 7" in result["results"][0]["error"]
    assert result["results"][1]["exact"] == 37
    assert route.call_count == 2


async def test_frequencies_partial_failure(api, client):
    route = api.post("/topRequests").mock(
        side_effect=by_phrase(
            COUNTS
            | {
                "бизнес коуч": httpx.Response(400, json={"code": 3, "message": "Bad phrase"}),
                '"!бизнес !коуч"': httpx.Response(400, json={"code": 3, "message": "Bad phrase"}),
                '"!нет !такого"': httpx.Response(503, text="upstream down"),
            }
        )
    )
    result = await client.frequencies(
        ["карьерный коуч", "бизнес коуч", "нет такого"], "both", None, None
    )

    ok, failed, half = result["results"]
    assert ok == {"phrase": "карьерный коуч", "base": 821, "exact": 37, "exact_share_percent": 4.5}
    assert failed["phrase"] == "бизнес коуч"
    assert "400: Bad phrase" in failed["error"]
    assert "base" not in failed and "exact" not in failed
    assert half["base"] == 0 and "exact" not in half
    assert half["error"].startswith("exact: Wordstat API error 503")
    assert route.call_count == 4 + 1 + 3  # the 503 is retried twice


async def test_frequencies_auth_error_fails_whole_call(api, client):
    api.post("/topRequests").respond(401, json={"code": 16, "message": "Unauthenticated"})
    with pytest.raises(WordstatError, match="401.*search-api.editor"):
        await client.frequencies(["карьерный коуч", "бизнес коуч"], "both", None, None)


# ---- retries -----------------------------------------------------------


async def test_rate_limit_code_8_is_retried(api, client):
    route = api.post("/topRequests").mock(
        side_effect=[
            httpx.Response(429, json={"code": 8, "message": "Too many requests"}),
            httpx.Response(200, json={"totalCount": "37"}),
        ]
    )
    result = await client.frequencies(["карьерный коуч"], "exact", None, None)
    assert result["results"] == [{"phrase": "карьерный коуч", "exact": 37}]
    assert route.call_count == 2


async def test_retries_stop_after_three_attempts(api, client):
    route = api.post("/topRequests").respond(429, json={"code": 8, "message": "Too many requests"})
    with pytest.raises(WordstatError, match="429: Too many requests"):
        await client.top_requests("a", 1, None, None)
    assert route.call_count == 3


async def test_client_errors_are_not_retried(api, client):
    route = api.post("/topRequests").respond(400, json={"code": 3, "message": "Bad Request"})
    with pytest.raises(WordstatError, match="400"):
        await client.top_requests("a", 1, None, None)
    assert route.call_count == 1


# ---- MCP tool ----------------------------------------------------------


async def test_frequencies_tool_resolves_regions_and_validates(api, monkeypatch):
    from yandex_wordstat_mcp import server

    api.post("/getRegionsTree").respond(
        json={"regions": [{"id": "225", "label": "Россия", "children": [{"id": "213", "label": "Москва"}]}]}
    )
    route = api.post("/topRequests").mock(side_effect=by_phrase(COUNTS))
    c = WordstatClient(CREDS)
    monkeypatch.setattr(server, "_client", c)
    try:
        result = await server.mcp.call_tool(
            "frequencies", {"phrases": ["карьерный коуч"], "regions": ["Москва"], "mode": "exact"}
        )
        assert "37" in str(result)
        assert json.loads(route.calls.last.request.content)["regions"] == ["213"]
        with pytest.raises(Exception):
            await server.mcp.call_tool("frequencies", {"phrases": []})
    finally:
        await c.aclose()
