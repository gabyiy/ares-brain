from network.http_client import RateLimitedHttpClient
from network.cache import TTLCache


class WeatherProvider:
    GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
    WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, http: RateLimitedHttpClient, cache: TTLCache):
        self.http = http
        self.cache = cache

    def current(self, city: str):
        city = city.strip()
        if not city:
            raise ValueError("City is required")

        key = f"weather:current:{city.lower()}"
        cached = self.cache.get(key)
        if cached:
            return cached

        geo = self.http.get_json(
            self.GEO_URL,
            params={"name": city, "count": 1, "language": "en", "format": "json"},
            service_delay=1.0,
        )

        results = geo.get("results") or []
        if not results:
            raise ValueError(f"City not found: {city}")

        place = results[0]
        lat = place["latitude"]
        lon = place["longitude"]

        data = self.http.get_json(
            self.WEATHER_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
                "timezone": "auto",
            },
            service_delay=1.0,
        )

        current = data.get("current", {})
        result = {
            "city": place.get("name"),
            "country": place.get("country"),
            "temperature": current.get("temperature_2m"),
            "humidity": current.get("relative_humidity_2m"),
            "wind": current.get("wind_speed_10m"),
            "weather_code": current.get("weather_code"),
        }

        self.cache.set(key, result, ttl_seconds=60 * 10)
        return result
