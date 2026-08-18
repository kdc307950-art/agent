import asyncio

from src.my_agent.tools import _request_weather, calculate, get_weather


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self):
        self.calls = []

    async def get(self, url, params):
        self.calls.append((url, params))
        if len(self.calls) == 1:
            return FakeResponse({"results": [{"name": "北京", "country": "中国", "latitude": 39.9, "longitude": 116.4}]})
        return FakeResponse(
            {
                "current": {
                    "temperature_2m": 25,
                    "apparent_temperature": 26,
                    "weather_code": 0,
                    "wind_speed_10m": 8,
                }
            }
        )


def test_weather_adapter_parses_geocoding_and_forecast():
    async def run():
        client = FakeClient()
        result = await _request_weather("北京", client)
        return result, client.calls

    result, calls = asyncio.run(run())
    assert "北京" in result
    assert "晴" in result
    assert "25°C" in result
    assert len(calls) == 2
    assert calls[0][1]["name"] == "北京"
    assert calls[1][1]["latitude"] == 39.9


def test_weather_tool_rejects_blank_or_oversized_location():
    async def run():
        blank = await get_weather.ainvoke({"location": "  "})
        oversized = await get_weather.ainvoke({"location": "x" * 129})
        return blank, oversized

    blank, oversized = asyncio.run(run())
    assert "不能为空" in blank
    assert "128" in oversized


def test_calculator_remains_deterministic_and_safe():
    assert calculate.invoke({"expression": "2 + 2 * 3"}) == "计算结果: 8"
    assert "非法字符" in calculate.invoke({"expression": "__import__('os')"})
