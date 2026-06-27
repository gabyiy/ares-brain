from datetime import datetime
from network.http_client import RateLimitedHttpClient
from network.cache import TTLCache


class WeatherProvider:
    GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
    WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

    WEATHER_CODES = {
        0: "clear sky",
        1: "mainly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "fog",
        48: "freezing fog",
        51: "light drizzle",
        53: "moderate drizzle",
        55: "heavy drizzle",
        61: "light rain",
        63: "moderate rain",
        65: "heavy rain",
        71: "light snow",
        73: "moderate snow",
        75: "heavy snow",
        80: "light rain showers",
        81: "moderate rain showers",
        82: "heavy rain showers",
        95: "thunderstorm",
    }

    ICONS = {
        0: "☀️",
        1: "🌤",
        2: "⛅",
        3: "☁️",
        45: "🌫",
        48: "🌫",
        51: "🌦",
        53: "🌦",
        55: "🌧",
        61: "🌧",
        63: "🌧",
        65: "⛈",
        71: "🌨",
        73: "🌨",
        75: "❄️",
        80: "🌦",
        81: "🌧",
        82: "⛈",
        95: "⛈",
    }

    def __init__(self, http: RateLimitedHttpClient, cache: TTLCache):
        self.http = http
        self.cache = cache

    def _weather_text(self, code):
        return self.WEATHER_CODES.get(code, "unknown weather")

    def _icon(self, code):
        return self.ICONS.get(code, "🌡")

    def _day_name(self, date_text):
        try:
            return datetime.strptime(date_text, "%Y-%m-%d").strftime("%A")
        except Exception:
            return date_text

    def _geo(self, city: str):
        geo = self.http.get_json(
            self.GEO_URL,
            params={"name": city, "count": 1, "language": "en", "format": "json"},
            service_delay=1.0,
        )

        results = geo.get("results") or []
        if not results:
            raise ValueError(f"City not found: {city}")

        return results[0]

    def forecast(self, city: str, mode: str = "today"):
        city = city.strip()
        if not city:
            raise ValueError("City is required")

        mode = mode.lower().strip()
        key = f"weather:forecast:{city.lower()}:{mode}"

        cached = self.cache.get(key)
        if cached:
            return cached

        place = self._geo(city)
        lat = place["latitude"]
        lon = place["longitude"]

        data = self.http.get_json(
            self.WEATHER_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
                "timezone": "auto",
                "forecast_days": 7,
            },
            service_delay=1.0,
        )

        result = {
            "city": place.get("name"),
            "country": place.get("country"),
            "mode": mode,
            "current": data.get("current", {}),
            "daily": data.get("daily", {}),
        }

        self.cache.set(key, result, ttl_seconds=60 * 10)
        return result

    def format_forecast(self, city: str, mode: str = "today"):
        data = self.forecast(city, mode)
        current = data.get("current", {})
        daily = data.get("daily", {})

        times = daily.get("time", [])
        codes = daily.get("weather_code", [])
        max_t = daily.get("temperature_2m_max", [])
        min_t = daily.get("temperature_2m_min", [])
        rain = daily.get("precipitation_probability_max", [])
        wind = daily.get("wind_speed_10m_max", [])

        mode = data.get("mode", "today")
        place = f"{data.get('city')}, {data.get('country')}"

        if mode == "now":
            code = current.get("weather_code")
            return (
                f"Weather in {place} right now:\n"
                f"{self._icon(code)} {self._weather_text(code)}\n"
                f"Temperature: {current.get('temperature_2m')}°C\n"
                f"Humidity: {current.get('relative_humidity_2m')}%\n"
                f"Wind: {current.get('wind_speed_10m')} km/h"
            )

        if mode == "week":
            lines = [f"Weather in {place} next week:"]
            for i in range(min(7, len(times))):
                code = codes[i]
                day = self._day_name(times[i])
                lines.append(
                    f"{day}: {self._icon(code)} {max_t[i]}°C / {min_t[i]}°C, "
                    f"{self._weather_text(code)}, rain {rain[i]}%, wind {wind[i]} km/h"
                )
            return "\n".join(lines)

        if mode == "tomorrow":
            index = 1
            label = "tomorrow"
        else:
            index = 0
            label = "today"

        code = codes[index]

        return (
            f"Weather in {place} {label}:\n"
            f"{self._icon(code)} {self._weather_text(code)}\n"
            f"High: {max_t[index]}°C\n"
            f"Low: {min_t[index]}°C\n"
            f"Rain chance: {rain[index]}%\n"
            f"Wind: {wind[index]} km/h"
        )
