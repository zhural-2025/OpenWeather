import requests
from dotenv import load_dotenv
import os
import json
import time
from datetime import datetime, timedelta, timezone

load_dotenv()
API_KEY = os.getenv("API_KEY")

GEOCODING_URL = "http://api.openweathermap.org/geo/1.0/direct"
CURRENT_WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"
CACHE_FILE = "weather_cache.json"
CACHE_MAX_AGE = timedelta(hours=3)
RETRY_DELAYS_SECONDS = (1, 2, 4)


def request_with_retries(
    url: str, params: dict, service_name: str
) -> tuple[requests.Response | None, bool]:
    had_network_error = False
    response = None

    for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
        try:
            response = requests.get(url, params=params, timeout=10)
        except requests.RequestException:
            had_network_error = True
            if attempt < len(RETRY_DELAYS_SECONDS):
                print(
                    f"Временная сетевая ошибка ({service_name}), повтор через {delay} сек."
                )
                time.sleep(delay)
                continue
            print(f"Ошибка сети: не удалось получить ответ ({service_name}).")
            return (None, True)

        if response.status_code == 429:
            if attempt < len(RETRY_DELAYS_SECONDS):
                print(f"Превышен лимит запросов, повтор через {delay} сек.")
                time.sleep(delay)
                continue
            print("Ошибка: превышен лимит запросов OpenWeather (429).")
            return (None, had_network_error)

        return (response, had_network_error)

    return (None, had_network_error)


def get_coordinates(city: str) -> tuple[float, float]:
    if not API_KEY:
        print("Ошибка: API_KEY не найден в .env.")
        return (0.0, 0.0)

    params = {
        "q": city,
        "limit": 1,
        "lang": "ru",
        "appid": API_KEY,
    }

    response, _ = request_with_retries(GEOCODING_URL, params, "Geocoding")
    if not response:
        return (0.0, 0.0)

    if response.status_code == 401:
        print("Ошибка: невалидный API-ключ OpenWeather.")
        return (0.0, 0.0)

    if response.status_code != 200:
        print(f"Ошибка OpenWeather Geocoding: статус {response.status_code}.")
        return (0.0, 0.0)

    data = response.json()
    if not data:
        print(f"Город '{city}' не найден. Проверьте название.")
        return (0.0, 0.0)

    return (data[0]["lat"], data[0]["lon"])


def get_weather_by_coordinates(lat: float, lon: float) -> dict:
    if not API_KEY:
        print("Ошибка: API_KEY не найден в .env.")
        return {}

    params = {
        "lat": lat,
        "lon": lon,
        "appid": API_KEY,
        "units": "metric",
        "lang": "ru",
    }

    response, had_network_error = request_with_retries(
        CURRENT_WEATHER_URL, params, "Current Weather"
    )
    if not response:
        if not had_network_error:
            return {}

        cached_data = load_cache()
        if cached_data and is_cache_fresh(cached_data):
            cached_city = cached_data.get("city", "неизвестный город")
            use_cache = input(
                f"Показать последние данные из кэша для '{cached_city}'? (y/n): "
            ).strip().lower()
            if use_cache in ("y", "yes", "д", "да"):
                print("Показываю данные из кэша.")
                return cached_data.get("weather", {})
        return {}

    if response.status_code == 401:
        print("Ошибка: невалидный API-ключ OpenWeather.")
        return {}

    if response.status_code != 200:
        print(f"Ошибка OpenWeather Current Weather: статус {response.status_code}.")
        return {}

    return response.json()


def print_weather(city_label: str, weather: dict) -> None:
    temperature = weather.get("main", {}).get("temp", "n/a")
    description = weather.get("weather", [{}])[0].get("description", "без описания")
    print(f"Погода в {city_label}: {temperature}°C, {description}")


def save_cache(city: str, lat: float, lon: float, weather: dict) -> None:
    cache_payload = {
        "city": city,
        "lat": lat,
        "lon": lon,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "weather": weather,
    }
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as file:
            json.dump(cache_payload, file, ensure_ascii=False, indent=2)
    except OSError:
        print("Предупреждение: не удалось сохранить кэш.")


def load_cache() -> dict:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}


def is_cache_fresh(cache_data: dict) -> bool:
    fetched_at = cache_data.get("fetched_at")
    if not fetched_at:
        return False
    try:
        fetched_time = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - fetched_time <= CACHE_MAX_AGE


if __name__ == "__main__":
    while True:
        print("\nВыберите режим:")
        print("1 — по городу")
        print("2 — по координатам")
        print("0 — выход")
        mode = input("Ваш выбор: ").strip()

        if mode == "0":
            print("Выход из программы.")
            break

        if mode == "1":
            city = input("Введите город: ").strip()
            if not city:
                print("Ошибка: город не может быть пустым.")
                continue

            latitude, longitude = get_coordinates(city)
            if latitude == 0.0 and longitude == 0.0:
                continue

            weather = get_weather_by_coordinates(latitude, longitude)
            if not weather:
                continue

            save_cache(city, latitude, longitude, weather)
            print_weather(city, weather)
            continue

        if mode == "2":
            lat_input = input("Введите широту (lat): ").strip()
            lon_input = input("Введите долготу (lon): ").strip()
            try:
                latitude = float(lat_input)
                longitude = float(lon_input)
            except ValueError:
                print("Ошибка: координаты должны быть числами.")
                continue

            weather = get_weather_by_coordinates(latitude, longitude)
            if not weather:
                continue

            city_name = weather.get("name", f"{latitude}, {longitude}")
            save_cache(city_name, latitude, longitude, weather)
            print_weather(city_name, weather)
            continue

        print("Неизвестный режим. Введите 1, 2 или 0.")