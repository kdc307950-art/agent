from __future__ import annotations

import ast
import os
from typing import Any

import httpx
from langchain_core.tools import tool


_GEOCODING_URL = os.getenv(
    "WEATHER_GEOCODING_URL",
    "https://geocoding-api.open-meteo.com/v1/search",
)
_FORECAST_URL = os.getenv(
    "WEATHER_FORECAST_URL",
    "https://api.open-meteo.com/v1/forecast",
)
_HTTP_TIMEOUT_SECONDS = float(os.getenv("TOOL_HTTP_TIMEOUT_SECONDS", "5"))

_WEATHER_CODES = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "冻雾",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    95: "雷雨",
    96: "雷雨伴冰雹",
    99: "强雷雨伴冰雹",
}


async def _request_weather(location: str, client: Any) -> str:
    geocoding = await client.get(
        _GEOCODING_URL,
        params={"name": location, "count": 1, "language": "zh", "format": "json"},
    )
    geocoding.raise_for_status()
    locations = geocoding.json().get("results") or []
    if not locations:
        return f"未找到地点：{location}"

    place = locations[0]
    forecast = await client.get(
        _FORECAST_URL,
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "timezone": "auto",
        },
    )
    forecast.raise_for_status()
    current = forecast.json().get("current") or {}
    code = int(current.get("weather_code", -1))
    return (
        f"{place.get('name', location)}（{place.get('country', '')}）当前天气："
        f"{_WEATHER_CODES.get(code, '未知天气')}，"
        f"气温 {current.get('temperature_2m', '未知')}°C，"
        f"体感 {current.get('apparent_temperature', '未知')}°C，"
        f"风速 {current.get('wind_speed_10m', '未知')} km/h。"
        "数据源：Open-Meteo。"
    )


@tool
async def get_weather(location: str) -> str:
    """查询城市当前天气，location 传入城市或地区名称。"""
    location = location.strip()
    if not location or len(location) > 128:
        return "错误：地点名称不能为空且不能超过 128 个字符"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            return await _request_weather(location, client)
    except httpx.TimeoutException:
        return "错误：天气服务请求超时，请稍后重试"
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return "错误：天气服务暂时不可用，请稍后重试"


@tool
def calculate(expression: str) -> str:
    """安全计算仅包含数字和 + - * / 括号的数学表达式。"""
    allowed_chars = set("0123456789+-*/() .")
    if not expression or not all(char in allowed_chars for char in expression):
        return "错误：表达式包含非法字符"

    try:
        tree = ast.parse(expression, mode="eval")
        allowed_nodes = (
            ast.Expression,
            ast.BinOp,
            ast.Add,
            ast.Sub,
            ast.Mult,
            ast.Div,
            ast.Constant,
            ast.Load,
            ast.UnaryOp,
            ast.USub,
            ast.UAdd,
        )
        if any(not isinstance(node, allowed_nodes) for node in ast.walk(tree)):
            return "错误：不支持的运算符或语法"
        result = eval(compile(tree, "<calculator>", "eval"), {"__builtins__": {}}, {})
        return f"计算结果: {result}"
    except SyntaxError:
        return "错误：表达式语法不正确"
    except ZeroDivisionError:
        return "错误：除数不能为零"
    except Exception as exc:
        return f"计算错误: {exc}"


tools = [get_weather, calculate]
