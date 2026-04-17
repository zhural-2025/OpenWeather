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
FORECAST_WEATHER_URL = "https://api.openweathermap.org/data/2.5/forecast"
AIR_POLLUTION_URL = "http://api.openweathermap.org/data/2.5/air_pollution"
CACHE_FILE = "weather_cache.json"
CACHE_MAX_AGE = timedelta(hours=3)
RETRY_DELAYS_SECONDS = (1, 2, 4)

# Границы концентраций по индексам 1..5 (таблица из задания), мкг/м3.
AIR_QUALITY_LIMITS = {
    "so2": [20, 80, 250, 350],
    "no2": [40, 70, 150, 200],
    "pm10": [20, 50, 100, 200],
    "pm2_5": [10, 25, 50, 75],
    "o3": [60, 100, 140, 180],
    "co": [4400, 9400, 12400, 15400],
}
AIR_QUALITY_LABELS = {
    1: "Good",
    2: "Fair",
    3: "Moderate",
    4: "Poor",
    5: "Very Poor",
}
POLLUTANT_DISPLAY_NAMES = {
    "so2": "SO2",
    "no2": "NO2",
    "pm10": "PM10",
    "pm2_5": "PM2.5",
    "o3": "O3",
    "co": "CO",
}


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


def get_coordinates(city: str) -> tuple[float, float] | None:
    if not API_KEY:
        print("Ошибка: API_KEY не найден в .env.")
        return None

    params = {
        "q": city,
        "limit": 1,
        "lang": "ru",
        "appid": API_KEY,
    }

    response, _ = request_with_retries(GEOCODING_URL, params, "Geocoding")
    if not response:
        return None

    if response.status_code == 401:
        print("Ошибка: невалидный API-ключ OpenWeather.")
        return None

    if response.status_code != 200:
        print(f"Ошибка OpenWeather Geocoding: статус {response.status_code}.")
        return None

    data = response.json()
    if not data:
        print(f"Город '{city}' не найден. Проверьте название.")
        return None

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


def get_hourly_forecast_by_coordinates(lat: float, lon: float) -> list[dict]:
    if not API_KEY:
        print("Ошибка: API_KEY не найден в .env.")
        return []

    params = {
        "lat": lat,
        "lon": lon,
        "appid": API_KEY,
        "units": "metric",
        "lang": "ru",
    }

    response, _ = request_with_retries(
        FORECAST_WEATHER_URL, params, "5 day / 3 hour Forecast"
    )
    if not response:
        return []

    if response.status_code == 401:
        print("Ошибка: невалидный API-ключ OpenWeather.")
        return []

    if response.status_code != 200:
        print(f"Ошибка OpenWeather Forecast: статус {response.status_code}.")
        return []

    return response.json().get("list", [])

def print_hourly_forecast(city_label: str, forecast_points: list[dict]) -> None:
    if not forecast_points:
        return

    print(f"\nПрогноз на 5 дней (шаг 3 часа) для {city_label}:")
    for point in forecast_points:
        dt_value = point.get("dt_txt", "n/a")
        temperature = point.get("main", {}).get("temp", "n/a")
        description = point.get("weather", [{}])[0].get("description", "без описания")
        print(f"{dt_value}: {temperature}°C, {description}")


def get_air_pollution(latitude: float, longitude: float) -> dict:
    if not API_KEY:
        print("Ошибка: API_KEY не найден в .env.")
        return {}

    params = {
        "lat": latitude,
        "lon": longitude,
        "appid": API_KEY,
    }

    response, _ = request_with_retries(AIR_POLLUTION_URL, params, "Air Pollution")
    if not response:
        return {}

    if response.status_code == 401:
        print("Ошибка: невалидный API-ключ OpenWeather.")
        return {}

    if response.status_code != 200:
        print(f"Ошибка OpenWeather Air Pollution: статус {response.status_code}.")
        return {}

    data = response.json()
    pollution_list = data.get("list", [])
    if not pollution_list:
        return {}
    return pollution_list[0]


def _pollutant_index(value: float, limits: list[float]) -> int:
    for idx, max_value in enumerate(limits, start=1):
        if value <= max_value:
            return idx
    return 5


def analyze_air_pollution(air_pollution: dict) -> str:
    # air_pollution = {
    #   "main": {"aqi": 2},
    #   "components": {
    #       "co": 108.25, "no": 0.09, "no2": 1.14, "o3": 59.12,
    #       "so2": 0.73, "pm2_5": 0.5, "pm10": 0.5, "nh3": 0.15
    #   }
    # }
    components = air_pollution.get("components", {})
    if not components:
        return "Нет данных по загрязнению воздуха."

    lines: list[str] = []
    overall_index = 1

    for pollutant, limits in AIR_QUALITY_LIMITS.items():
        value = components.get(pollutant)
        if value is None:
            continue

        index = _pollutant_index(float(value), limits)
        overall_index = max(overall_index, index)

        good_limit = limits[0]
        display_name = POLLUTANT_DISPLAY_NAMES[pollutant]
        if value <= good_limit:
            lines.append(
                f"- {display_name}: {value:.2f} мкг/м3 (ниже нормы Good <= {good_limit})"
            )
        else:
            exceed = value - good_limit
            lines.append(
                f"- {display_name}: {value:.2f} мкг/м3 (выше нормы Good на {exceed:.2f}, "
                f"уровень {AIR_QUALITY_LABELS[index]})"
            )

    status = AIR_QUALITY_LABELS.get(overall_index, "Unknown")
    result_lines = [
        f"Качество воздуха: {status} (индекс {overall_index}/5).",
        "Расширенная информация:",
        *lines,
    ]
    return "\n".join(result_lines)


def print_air_pollution(city_label: str, air_pollution: dict) -> None:
    if not air_pollution:
        print(f"\nДанные о загрязнении воздуха для {city_label} недоступны.")
        return
    print(f"\nЗагрязнение воздуха для {city_label}:")
    print(analyze_air_pollution(air_pollution))


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

            coordinates = get_coordinates(city)
            if coordinates is None:
                continue
            latitude, longitude = coordinates

            weather = get_weather_by_coordinates(latitude, longitude)
            if not weather:
                continue

            save_cache(city, latitude, longitude, weather)
            print_weather(city, weather)
            forecast = get_hourly_forecast_by_coordinates(latitude, longitude)
            print_hourly_forecast(city, forecast)
            air_pollution = get_air_pollution(latitude, longitude)
            print_air_pollution(city, air_pollution)
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
            forecast = get_hourly_forecast_by_coordinates(latitude, longitude)
            print_hourly_forecast(city_name, forecast)
            air_pollution = get_air_pollution(latitude, longitude)
            print_air_pollution(city_name, air_pollution)
            continue

        print("Неизвестный режим. Введите 1, 2 или 0.")