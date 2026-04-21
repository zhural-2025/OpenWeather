import requests
from dotenv import load_dotenv
import os
import json
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

load_dotenv()
API_KEY = os.getenv("OW_API_KEY") or os.getenv("API_KEY")

GEOCODING_URL = "http://api.openweathermap.org/geo/1.0/direct"
CURRENT_WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"
FORECAST_WEATHER_URL = "https://api.openweathermap.org/data/2.5/forecast"
AIR_POLLUTION_URL = "http://api.openweathermap.org/data/2.5/air_pollution"
CACHE_FILE = "weather_cache.json"
CACHE_MAX_AGE = timedelta(hours=3)
RETRY_DELAYS_SECONDS = (1, 2, 4)
API_CACHE_DIR = Path(".cache")
API_CACHE_MAX_AGE = timedelta(minutes=10)

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
AIR_QUALITY_LABELS_RU = {
    1: "Хорошо",
    2: "Удовлетворительно",
    3: "Умеренно",
    4: "Плохо",
    5: "Очень плохо",
}
POLLUTANT_DISPLAY_NAMES = {
    "so2": "SO2",
    "no2": "NO2",
    "pm10": "PM10",
    "pm2_5": "PM2.5",
    "o3": "O3",
    "co": "CO",
}
WEATHER_DESCRIPTION_TRANSLATIONS = {
    "clear sky": "ясно",
    "few clouds": "малооблачно",
    "scattered clouds": "переменная облачность",
    "broken clouds": "облачно с прояснениями",
    "overcast clouds": "пасмурно",
    "mist": "туман",
    "smoke": "дымка",
    "haze": "мгла",
    "fog": "туман",
    "sand": "песчаная пыль",
    "dust": "пыль",
    "ash": "пепел",
    "squall": "шквал",
    "tornado": "торнадо",
    "light rain": "небольшой дождь",
    "moderate rain": "умеренный дождь",
    "heavy intensity rain": "сильный дождь",
    "very heavy rain": "очень сильный дождь",
    "extreme rain": "ливень",
    "freezing rain": "ледяной дождь",
    "light intensity shower rain": "небольшой ливень",
    "shower rain": "ливень",
    "heavy intensity shower rain": "сильный ливень",
    "ragged shower rain": "местами ливень",
    "light snow": "небольшой снег",
    "snow": "снег",
    "heavy snow": "сильный снег",
    "sleet": "дождь со снегом",
    "light shower sleet": "небольшой мокрый снег",
    "shower sleet": "мокрый снег",
    "light rain and snow": "небольшой дождь со снегом",
    "rain and snow": "дождь со снегом",
    "light shower snow": "небольшой снегопад",
    "shower snow": "снегопад",
    "heavy shower snow": "сильный снегопад",
    "thunderstorm": "гроза",
    "thunderstorm with light rain": "гроза с небольшим дождем",
    "thunderstorm with rain": "гроза с дождем",
    "thunderstorm with heavy rain": "гроза с сильным дождем",
    "light thunderstorm": "небольшая гроза",
    "heavy thunderstorm": "сильная гроза",
    "ragged thunderstorm": "местами гроза",
    "thunderstorm with light drizzle": "гроза с моросью",
    "thunderstorm with drizzle": "гроза с моросью",
    "thunderstorm with heavy drizzle": "гроза с сильной моросью",
    "light intensity drizzle": "небольшая морось",
    "drizzle": "морось",
    "heavy intensity drizzle": "сильная морось",
    "light intensity drizzle rain": "небольшой дождь с моросью",
    "drizzle rain": "дождь с моросью",
    "heavy intensity drizzle rain": "сильный дождь с моросью",
    "shower rain and drizzle": "ливень с моросью",
    "heavy shower rain and drizzle": "сильный ливень с моросью",
    "shower drizzle": "ливневая морось",
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


def _api_cache_path(lat: float, lon: float, endpoint: str) -> Path:
    API_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lat_key = f"{lat:.6f}"
    lon_key = f"{lon:.6f}"
    return API_CACHE_DIR / f"{endpoint}_{lat_key}_{lon_key}.json"


def _load_api_cache(lat: float, lon: float, endpoint: str) -> dict | list | None:
    cache_path = _api_cache_path(lat, lon, endpoint)
    try:
        with open(cache_path, "r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    fetched_at = payload.get("fetched_at")
    if not fetched_at:
        return None
    try:
        fetched_dt = datetime.fromisoformat(fetched_at)
    except ValueError:
        return None
    if datetime.now(timezone.utc) - fetched_dt > API_CACHE_MAX_AGE:
        return None
    return payload.get("data")


def _save_api_cache(lat: float, lon: float, endpoint: str, data: dict | list) -> None:
    cache_path = _api_cache_path(lat, lon, endpoint)
    payload = {
        "lat": lat,
        "lon": lon,
        "endpoint": endpoint,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    try:
        with open(cache_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
    except OSError:
        print("Предупреждение: не удалось сохранить API-кэш.")


def _translate_weather_description_ru(description: str) -> str:
    if not description:
        return description
    normalized = description.strip().lower()
    return WEATHER_DESCRIPTION_TRANSLATIONS.get(normalized, description)


def _localize_weather_payload_inplace(payload: dict | list) -> None:
    if isinstance(payload, dict):
        weather_list = payload.get("weather")
        if isinstance(weather_list, list):
            for item in weather_list:
                if isinstance(item, dict) and "description" in item:
                    item["description"] = _translate_weather_description_ru(
                        str(item.get("description", ""))
                    )
        forecast_list = payload.get("list")
        if isinstance(forecast_list, list):
            for point in forecast_list:
                if isinstance(point, dict):
                    _localize_weather_payload_inplace(point)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                _localize_weather_payload_inplace(item)


def get_coordinates(city: str, limit: int = 1) -> tuple[float, float] | None:
    if not API_KEY:
        print("Ошибка: OW_API_KEY (или API_KEY) не найден в .env.")
        return None

    params = {
        "q": city,
        "limit": limit,
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


def get_current_weather(lat: float, lon: float) -> dict:
    if not API_KEY:
        print("Ошибка: OW_API_KEY (или API_KEY) не найден в .env.")
        return {}

    cached = _load_api_cache(lat, lon, "current")
    if isinstance(cached, dict):
        _localize_weather_payload_inplace(cached)
        return cached

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
        if had_network_error:
            print("Предупреждение: используйте последнюю доступную кэш-копию, если есть.")
        return {}

    if response.status_code == 401:
        print("Ошибка: невалидный API-ключ OpenWeather.")
        return {}

    if response.status_code != 200:
        print(f"Ошибка OpenWeather Current Weather: статус {response.status_code}.")
        return {}

    data = response.json()
    _localize_weather_payload_inplace(data)
    _save_api_cache(lat, lon, "current", data)
    return data


def get_weather_by_coordinates(lat: float, lon: float) -> dict:
    return get_current_weather(lat, lon)


def print_weather(city_label: str, weather: dict) -> None:
    temperature = weather.get("main", {}).get("temp", "n/a")
    description = weather.get("weather", [{}])[0].get("description", "без описания")
    print(f"Погода в {city_label}: {temperature}°C, {description}")


def get_forecast_5d3h(lat: float, lon: float) -> list[dict]:
    if not API_KEY:
        print("Ошибка: OW_API_KEY (или API_KEY) не найден в .env.")
        return []

    cached = _load_api_cache(lat, lon, "forecast_5d3h")
    if isinstance(cached, list):
        _localize_weather_payload_inplace(cached)
        return cached

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

    data = response.json().get("list", [])
    _localize_weather_payload_inplace(data)
    _save_api_cache(lat, lon, "forecast_5d3h", data)
    return data


def get_hourly_forecast_by_coordinates(lat: float, lon: float) -> list[dict]:
    return get_forecast_5d3h(lat, lon)

def print_hourly_forecast(city_label: str, forecast_points: list[dict]) -> None:
    if not forecast_points:
        return

    print(f"\nПрогноз на 5 дней (шаг 3 часа) для {city_label}:")
    for point in forecast_points:
        dt_value = point.get("dt_txt", "n/a")
        temperature = point.get("main", {}).get("temp", "n/a")
        description = point.get("weather", [{}])[0].get("description", "без описания")
        print(f"{dt_value}: {temperature}°C, {description}")


def get_air_pollution(lat: float, lon: float) -> dict:
    record = get_air_pollution_record(lat, lon)
    if not record:
        return {}
    components = record.get("components", {})
    return components if isinstance(components, dict) else {}


def get_air_pollution_record(lat: float, lon: float) -> dict | None:
    if not API_KEY:
        print("Ошибка: OW_API_KEY (или API_KEY) не найден в .env.")
        return None

    cached = _load_api_cache(lat, lon, "air_pollution")
    if isinstance(cached, dict):
        return cached

    params = {
        "lat": lat,
        "lon": lon,
        "appid": API_KEY,
    }

    response, _ = request_with_retries(AIR_POLLUTION_URL, params, "Air Pollution")
    if not response:
        return None

    if response.status_code == 401:
        print("Ошибка: невалидный API-ключ OpenWeather.")
        return None

    if response.status_code != 200:
        print(f"Ошибка OpenWeather Air Pollution: статус {response.status_code}.")
        return None

    data = response.json()
    pollution_list = data.get("list", [])
    if not pollution_list:
        return None
    record = pollution_list[0]
    if not isinstance(record, dict):
        return None
    _save_api_cache(lat, lon, "air_pollution", record)
    return record


def _pollutant_index(value: float, limits: list[float]) -> int:
    for idx, max_value in enumerate(limits, start=1):
        if value <= max_value:
            return idx
    return 5


def analyze_air_pollution(components: dict, extended: bool = False) -> dict:
    if not components:
        return {
            "status": "Неизвестно",
            "status_en": "Unknown",
            "overall_index": None,
            "message": "Нет данных по загрязнению воздуха.",
            "details": [],
            "pollutants": [],
        }

    details: list[str] = []
    pollutants: list[dict] = []
    overall_index = 1

    for pollutant, limits in AIR_QUALITY_LIMITS.items():
        value = components.get(pollutant)
        if value is None:
            continue

        index = _pollutant_index(float(value), limits)
        overall_index = max(overall_index, index)

        good_limit = limits[0]
        display_name = POLLUTANT_DISPLAY_NAMES[pollutant]
        status_ru = AIR_QUALITY_LABELS_RU.get(index, "неизвестно")
        if value <= good_limit:
            line = (
                f"- {display_name}: {value:.2f} мкг/м³ "
                f"(ниже порога «хорошо» ≤ {good_limit})"
            )
        else:
            exceed = value - good_limit
            line = (
                f"- {display_name}: {value:.2f} мкг/м³ "
                f"(выше порога «хорошо» на {exceed:.2f}, статус: {status_ru})"
            )

        details.append(line)
        pollutants.append(
            {
                "key": pollutant,
                "name": display_name,
                "value": float(value),
                "index": index,
                "label": AIR_QUALITY_LABELS[index],
                "label_ru": status_ru,
            }
        )

    status_en = AIR_QUALITY_LABELS.get(overall_index, "Unknown")
    status_ru = AIR_QUALITY_LABELS_RU.get(overall_index, "неизвестно")
    result: dict = {
        "status": status_ru,
        "status_en": status_en,
        "status_ru": status_ru,
        "overall_index": overall_index,
        "message": f"Качество воздуха: {status_ru} (индекс {overall_index}/5).",
        "details": details,
    }
    if extended:
        result["pollutants"] = pollutants
    return result


def format_air_pollution_analysis(analysis: dict) -> str:
    if not analysis.get("details"):
        return str(analysis.get("message", "Нет данных по загрязнению воздуха."))
    return "\n".join(
        [
            str(analysis.get("message", "")),
            "Детали:",
            *analysis.get("details", []),
        ]
    )


def print_air_pollution(city_label: str, air_pollution: dict) -> None:
    if not air_pollution:
        print(f"\nДанные о загрязнении воздуха для {city_label} недоступны.")
        return
    print(f"\nЗагрязнение воздуха для {city_label}:")
    if "components" in air_pollution:
        components = air_pollution.get("components", {})
    else:
        components = air_pollution
    print(format_air_pollution_analysis(analyze_air_pollution(components, extended=True)))


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
            air_pollution = get_air_pollution_record(latitude, longitude) or {}
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
            air_pollution = get_air_pollution_record(latitude, longitude) or {}
            print_air_pollution(city_name, air_pollution)
            continue

        print("Неизвестный режим. Введите 1, 2 или 0.")