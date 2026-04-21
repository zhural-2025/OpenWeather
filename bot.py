import json
import os
import threading
import time
from datetime import datetime
from urllib.parse import quote_plus

import requests
import telebot
from dotenv import load_dotenv
from telebot import types

from storage import USER_DATA_FILE, load_user, save_user
from weather_app import (
    API_KEY,
    analyze_air_pollution,
    format_air_pollution_analysis,
    get_air_pollution,
    get_air_pollution_record,
    get_coordinates,
    get_current_weather,
    get_forecast_5d3h,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set")
if not API_KEY:
    raise ValueError("OW_API_KEY (or API_KEY) is not set")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

ONE_CALL_URL = "https://api.openweathermap.org/data/3.0/onecall"
DEFAULT_NOTIFY_INTERVAL_H = 2
SCHEDULER_TICK_SECONDS = 60

BTN_START_MENU = "🏠 Меню"
BTN_CURRENT_WEATHER_CITY = "🌤 Текущая погода (город)"
BTN_CURRENT_WEATHER_GEO = "📡 Текущая погода (гео)"
BTN_MY_LOCATION = "📍 Моя геолокация"
BTN_FORECAST_5D = "📅 Прогноз на 5 дней"
BTN_COMPARE_CITIES = "🏙 Сравнить города"
BTN_EXTENDED = "🧪 Расширенные данные"
BTN_NOTIFICATIONS = "🔔 Уведомления"

user_locations: dict[int, tuple[float, float]] = {}
user_forecasts: dict[int, dict[str, list[dict]]] = {}
last_inline_message_id: dict[int, int] = {}
notification_subscriptions: dict[int, bool] = {}
notification_interval_hours: dict[int, int] = {}
last_notification_sent_at: dict[int, float] = {}
last_onupdate_check_at: dict[int, float] = {}
pending_location_action: dict[int, str] = {}
storage_lock = threading.Lock()


def load_user_data() -> None:
    global user_locations, user_forecasts, last_inline_message_id, notification_subscriptions
    global notification_interval_hours, last_notification_sent_at
    user_locations = {}
    user_forecasts = {}
    last_inline_message_id = {}
    notification_subscriptions = {}
    notification_interval_hours = {}
    last_notification_sent_at = {}

    if not os.path.exists(USER_DATA_FILE):
        return

    try:
        with open(USER_DATA_FILE, "r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return

    if not isinstance(payload, dict):
        return

    for user_id_raw, record in payload.items():
        if not str(user_id_raw).isdigit():
            continue
        if not isinstance(record, dict):
            continue
        user_id = int(user_id_raw)
        lat = record.get("lat")
        lon = record.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            user_locations[user_id] = (float(lat), float(lon))

        notifications = record.get("notifications")
        if isinstance(notifications, dict):
            notification_subscriptions[user_id] = bool(notifications.get("enabled", False))
            interval_h = notifications.get("interval_h", DEFAULT_NOTIFY_INTERVAL_H)
            try:
                interval_int = int(interval_h)
            except (TypeError, ValueError):
                interval_int = DEFAULT_NOTIFY_INTERVAL_H
            interval_int = max(1, min(24, interval_int))
            notification_interval_hours[user_id] = interval_int

        extras = record.get("_extras")
        if isinstance(extras, dict):
            forecast = extras.get("forecast_by_day")
            if isinstance(forecast, dict):
                user_forecasts[user_id] = forecast
            last_id = extras.get("last_inline_message_id")
            if isinstance(last_id, int):
                last_inline_message_id[user_id] = last_id
            last_sent = extras.get("last_notification_sent_at")
            if isinstance(last_sent, (int, float)):
                last_notification_sent_at[user_id] = float(last_sent)


def persist_user(user_id: int, patch: dict | None = None) -> None:
    record = dict(load_user(user_id))
    if patch:
        record.update(patch)
    lat, lon = user_locations.get(user_id, (None, None))
    if lat is not None and lon is not None:
        record["lat"] = lat
        record["lon"] = lon

    notifications = record.get("notifications")
    if not isinstance(notifications, dict):
        notifications = {}

    if isinstance(patch, dict) and "notifications" in patch and isinstance(
        patch.get("notifications"), dict
    ):
        notifications.update(patch["notifications"])

    notifications["enabled"] = bool(notification_subscriptions.get(user_id, False))

    interval_h = notifications.get("interval_h")
    if user_id in notification_interval_hours:
        interval_h = notification_interval_hours[user_id]
    try:
        interval_int = int(interval_h) if interval_h is not None else DEFAULT_NOTIFY_INTERVAL_H
    except (TypeError, ValueError):
        interval_int = DEFAULT_NOTIFY_INTERVAL_H
    interval_int = max(1, min(24, interval_int))
    notifications["interval_h"] = interval_int
    notification_interval_hours[user_id] = interval_int
    record["notifications"] = notifications

    extras = dict(record.get("_extras", {})) if isinstance(record.get("_extras"), dict) else {}
    if user_id in user_forecasts and user_forecasts[user_id]:
        extras["forecast_by_day"] = user_forecasts[user_id]
    else:
        extras.pop("forecast_by_day", None)
    if user_id in last_inline_message_id:
        extras["last_inline_message_id"] = last_inline_message_id[user_id]
    else:
        extras.pop("last_inline_message_id", None)

    if user_id in last_notification_sent_at:
        extras["last_notification_sent_at"] = last_notification_sent_at[user_id]

    if extras:
        record["_extras"] = extras
    elif "_extras" in record:
        del record["_extras"]

    with storage_lock:
        save_user(user_id, record)


def build_main_keyboard() -> types.ReplyKeyboardMarkup:
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        types.KeyboardButton(BTN_CURRENT_WEATHER_CITY),
        types.KeyboardButton(BTN_CURRENT_WEATHER_GEO, request_location=True),
    )
    keyboard.add(types.KeyboardButton(BTN_MY_LOCATION, request_location=True))
    keyboard.add(types.KeyboardButton(BTN_FORECAST_5D), types.KeyboardButton(BTN_COMPARE_CITIES))
    keyboard.add(types.KeyboardButton(BTN_EXTENDED), types.KeyboardButton(BTN_NOTIFICATIONS))
    keyboard.add(types.KeyboardButton(BTN_START_MENU))
    return keyboard


def get_wind_kmh(weather: dict) -> float:
    speed_m_s = weather.get("wind", {}).get("speed", 0.0)
    return float(speed_m_s) * 3.6


def format_current_weather(city_name: str, weather: dict) -> str:
    main_data = weather.get("main", {})
    weather_data = weather.get("weather", [{}])[0]
    wind_data = weather.get("wind", {})
    clouds_data = weather.get("clouds", {})
    visibility = weather.get("visibility")

    visibility_km = (float(visibility) / 1000.0) if visibility is not None else None
    wind_kmh = get_wind_kmh(weather)
    return (
        f"🌤 <b>Погода в {city_name}</b>\n"
        f"• Температура: <b>{main_data.get('temp', 'n/a')}°C</b>\n"
        f"• Ощущается как: {main_data.get('feels_like', 'n/a')}°C\n"
        f"• Влажность: {main_data.get('humidity', 'n/a')}%\n"
        f"• Давление: {main_data.get('pressure', 'n/a')} гПа\n"
        f"• Ветер: {wind_data.get('speed', 'n/a')} м/с ({wind_kmh:.1f} км/ч)\n"
        f"• Облачность: {clouds_data.get('all', 'n/a')}%\n"
        f"• Видимость: {f'{visibility_km:.1f} км' if visibility_km is not None else 'n/a'}\n"
        f"• Условия: {weather_data.get('description', 'без описания').capitalize()}"
    )


def build_forecast_link(city_name: str, lat: float, lon: float) -> str:
    city_encoded = quote_plus(city_name)
    return f"https://openweathermap.org/find?q={city_encoded}&lat={lat:.4f}&lon={lon:.4f}"


def group_forecast_by_day(forecast_points: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for point in forecast_points:
        dt_txt = point.get("dt_txt")
        if not dt_txt:
            continue
        date_key = dt_txt.split(" ")[0]
        grouped.setdefault(date_key, []).append(point)
    return grouped


def build_days_inline_markup(day_keys: list[str]) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton(
            text=datetime.strptime(day_key, "%Y-%m-%d").strftime("%d.%m (%a)"),
            callback_data=f"day:{day_key}",
        )
        for day_key in day_keys
    ]
    markup.add(*buttons)
    return markup


def summarize_day(day_key: str, points: list[dict]) -> str:
    temperatures = [p.get("main", {}).get("temp") for p in points if p.get("main")]
    temps = [float(t) for t in temperatures if t is not None]
    descriptions: list[str] = []
    for point in points:
        description = point.get("weather", [{}])[0].get("description")
        if description:
            descriptions.append(description)

    most_common_desc = max(set(descriptions), key=descriptions.count) if descriptions else "n/a"
    min_temp = min(temps) if temps else "n/a"
    max_temp = max(temps) if temps else "n/a"
    return (
        f"📅 <b>{datetime.strptime(day_key, '%Y-%m-%d').strftime('%d.%m.%Y')}</b>\n"
        f"• Мин: {min_temp if isinstance(min_temp, str) else f'{min_temp:.1f}'}°C\n"
        f"• Макс: {max_temp if isinstance(max_temp, str) else f'{max_temp:.1f}'}°C\n"
        f"• Вероятные условия: {most_common_desc.capitalize()}"
    )


def detailed_day_text(day_key: str, points: list[dict]) -> str:
    lines = [
        f"🔎 <b>Детали на {datetime.strptime(day_key, '%Y-%m-%d').strftime('%d.%m.%Y')}</b>"
    ]
    for point in points:
        dt_txt = point.get("dt_txt", "")
        hour = dt_txt.split(" ")[1][:5] if " " in dt_txt else "n/a"
        temp = point.get("main", {}).get("temp", "n/a")
        humidity = point.get("main", {}).get("humidity", "n/a")
        wind_speed = point.get("wind", {}).get("speed", "n/a")
        description = point.get("weather", [{}])[0].get("description", "без описания")
        lines.append(
            f"\n🕒 <b>{hour}</b>\n"
            f"• Температура: {temp}°C\n"
            f"• Влажность: {humidity}%\n"
            f"• Ветер: {wind_speed} м/с\n"
            f"• Условия: {description.capitalize()}"
        )
    return "\n".join(lines)


def safe_delete_inline(chat_id: int, user_id: int) -> None:
    message_id = last_inline_message_id.get(user_id)
    if not message_id:
        return
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def send_days_menu(chat_id: int, user_id: int) -> None:
    forecast_days = user_forecasts.get(user_id, {})
    if not forecast_days:
        bot.send_message(
            chat_id,
            "Нет сохраненного прогноза. Сначала отправьте геолокацию через «Моя геолокация».",
        )
        return
    day_keys = sorted(forecast_days.keys())[:5]
    keyboard = build_days_inline_markup(day_keys)
    safe_delete_inline(chat_id, user_id)
    sent = bot.send_message(
        chat_id,
        "📅 <b>Прогноз на 5 дней</b>\nНажмите на день, чтобы посмотреть подробности:",
        reply_markup=keyboard,
    )
    last_inline_message_id[user_id] = sent.message_id
    persist_user(user_id)


def fetch_uv_index(lat: float, lon: float) -> float | None:
    params = {
        "lat": lat,
        "lon": lon,
        "appid": API_KEY,
        "units": "metric",
        "exclude": "minutely,hourly,daily,alerts",
    }
    try:
        response = requests.get(ONE_CALL_URL, params=params, timeout=10)
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    return response.json().get("current", {}).get("uvi")


def user_interval_seconds(user_id: int) -> int:
    hours = notification_interval_hours.get(user_id, DEFAULT_NOTIFY_INTERVAL_H)
    try:
        hours_int = int(hours)
    except (TypeError, ValueError):
        hours_int = DEFAULT_NOTIFY_INTERVAL_H
    hours_int = max(1, min(24, hours_int))
    return hours_int * 60 * 60


def build_notification_message(lat: float, lon: float) -> str | None:
    forecast_points = get_forecast_5d3h(lat, lon)
    if not forecast_points:
        return None
    now_weather = get_current_weather(lat, lon)
    if not now_weather:
        return None
    city_name = now_weather.get("name", "вашей локации")
    now_desc = now_weather.get("weather", [{}])[0].get("description", "").lower()

    rain_soon = False
    for point in forecast_points[:8]:
        description = point.get("weather", [{}])[0].get("description", "").lower()
        if "дожд" in description or "rain" in description:
            rain_soon = True
            break

    message_parts = [f"🔔 Обновление погоды для {city_name}."]
    if rain_soon:
        message_parts.append("В ближайшие часы ожидается дождь ☔.")
    message_parts.append(f"Сейчас: {now_desc.capitalize()}.")
    message_parts.append(f"Температура: {now_weather.get('main', {}).get('temp', 'n/a')}°C.")
    return "\n".join(message_parts)


def try_send_notification(user_id: int, *, force: bool = False) -> None:
    if not notification_subscriptions.get(user_id, False):
        return
    coords = user_locations.get(user_id)
    if not coords:
        return
    lat, lon = coords

    now_ts = time.time()
    interval_s = user_interval_seconds(user_id)
    last_sent = last_notification_sent_at.get(user_id)
    if not force and last_sent is not None and (now_ts - last_sent) < interval_s:
        return

    text = build_notification_message(lat, lon)
    if not text:
        return

    bot.send_message(user_id, text)
    last_notification_sent_at[user_id] = now_ts
    persist_user(user_id)


def maybe_notification_check_on_update(user_id: int) -> None:
    if not notification_subscriptions.get(user_id, False):
        return
    now_ts = time.time()
    last_tick = last_onupdate_check_at.get(user_id, 0.0)
    if now_ts - last_tick < 60:
        return
    last_onupdate_check_at[user_id] = now_ts

    interval_s = user_interval_seconds(user_id)
    last_sent = last_notification_sent_at.get(user_id)
    if last_sent is None or (now_ts - last_sent) >= interval_s:
        try_send_notification(user_id)


def format_extended_report(lat: float, lon: float, weather: dict) -> str:
    city_name = weather.get("name", f"{lat}, {lon}")
    sys_data = weather.get("sys", {})
    sunrise_raw = sys_data.get("sunrise")
    sunset_raw = sys_data.get("sunset")
    sunrise = datetime.fromtimestamp(sunrise_raw).strftime("%H:%M") if sunrise_raw else "n/a"
    sunset = datetime.fromtimestamp(sunset_raw).strftime("%H:%M") if sunset_raw else "n/a"
    uv_index = fetch_uv_index(lat, lon)
    air_record = get_air_pollution_record(lat, lon) or {}
    aqi = air_record.get("main", {}).get("aqi")
    components = air_record.get("components") or {}
    if not components:
        components = get_air_pollution(lat, lon)

    air_analysis = analyze_air_pollution(components, extended=True)
    air_text = format_air_pollution_analysis(air_analysis)

    return (
        f"{format_current_weather(city_name, weather)}\n\n"
        f"🧪 <b>Расширенные данные</b>\n"
        f"• UV-индекс: {uv_index if uv_index is not None else 'n/a'}\n"
        f"• Восход: {sunrise}\n"
        f"• Закат: {sunset}\n"
        f"• Индекс качества воздуха (AQI): {aqi if aqi is not None else 'n/a'}\n"
        f"• CO: {components.get('co', 'n/a')} мкг/м³\n"
        f"• NO2: {components.get('no2', 'n/a')} мкг/м³\n"
        f"• O3: {components.get('o3', 'n/a')} мкг/м³\n"
        f"• PM2.5: {components.get('pm2_5', 'n/a')} мкг/м³\n"
        f"• PM10: {components.get('pm10', 'n/a')} мкг/м³\n\n"
        f"🫁 <b>Анализ качества воздуха</b>\n{air_text}"
    )


def notification_worker() -> None:
    while True:
        for user_id, is_active in list(notification_subscriptions.items()):
            if not is_active:
                continue
            try_send_notification(user_id)
        time.sleep(SCHEDULER_TICK_SECONDS)


@bot.message_handler(commands=["start"])
def handle_start(message: types.Message) -> None:
    bot.send_message(
        message.chat.id,
        (
            "Привет! Я погодный бот.\n\n"
            "Выберите действие кнопками ниже.\n"
            "Коротко:\n"
            f"• {BTN_CURRENT_WEATHER_CITY} — ввод города\n"
            f"• {BTN_CURRENT_WEATHER_GEO} — погода по точке на карте\n"
            f"• {BTN_MY_LOCATION} — сохранить координаты для прогноза/уведомлений\n"
            f"• {BTN_FORECAST_5D} — прогноз на 5 дней (нужна сохраненная гео)\n"
            f"• {BTN_COMPARE_CITIES} — сравнение температур\n"
            f"• {BTN_EXTENDED} — расширенные данные + анализ воздуха\n"
            f"• {BTN_NOTIFICATIONS} — уведомления (интервал в часах)\n"
            f"• {BTN_START_MENU} — это меню еще раз"
        ),
        reply_markup=build_main_keyboard(),
    )


@bot.message_handler(func=lambda msg: msg.text == BTN_START_MENU)
def handle_menu_button(message: types.Message) -> None:
    handle_start(message)


@bot.message_handler(func=lambda msg: msg.text == BTN_CURRENT_WEATHER_CITY)
def handle_current_weather_city(message: types.Message) -> None:
    maybe_notification_check_on_update(message.from_user.id)
    prompt = bot.send_message(message.chat.id, "Введите название города:")
    bot.register_next_step_handler(prompt, process_city_weather)


def process_city_weather(message: types.Message) -> None:
    maybe_notification_check_on_update(message.from_user.id)
    city = (message.text or "").strip()
    if not city:
        bot.send_message(message.chat.id, "Название города пустое. Попробуйте снова.")
        return
    coordinates = get_coordinates(city)
    if not coordinates:
        bot.send_message(message.chat.id, "Город не найден.")
        return
    lat, lon = coordinates
    weather = get_current_weather(lat, lon)
    if not weather:
        bot.send_message(message.chat.id, "Не удалось получить погоду.")
        return
    user_id = message.from_user.id
    user_locations[user_id] = (lat, lon)
    persist_user(user_id, {"city": weather.get("name", city)})
    bot.send_message(message.chat.id, format_current_weather(weather.get("name", city), weather))


@bot.message_handler(content_types=["location"])
def handle_location(message: types.Message) -> None:
    maybe_notification_check_on_update(message.from_user.id)
    location = message.location
    lat, lon = float(location.latitude), float(location.longitude)
    user_id = message.from_user.id
    action = pending_location_action.pop(user_id, "save_location")

    if action == "current_weather_geo":
        weather = get_current_weather(lat, lon)
        if not weather:
            bot.send_message(message.chat.id, "Не удалось получить погоду по геолокации.")
            return
        user_locations[user_id] = (lat, lon)
        persist_user(user_id, {"city": weather.get("name", f"{lat}, {lon}")})
        city_name = weather.get("name", f"{lat}, {lon}")
        bot.send_message(message.chat.id, format_current_weather(city_name, weather))
        bot.send_message(message.chat.id, "Меню:", reply_markup=build_main_keyboard())
        return

    user_locations[user_id] = (lat, lon)
    weather = get_current_weather(lat, lon)
    city_name = weather.get("name", f"{lat}, {lon}") if weather else f"{lat}, {lon}"
    persist_user(user_id, {"city": city_name})
    bot.send_message(
        message.chat.id,
        "📍 Геолокация сохранена. Теперь можно открыть прогноз на 5 дней.",
        reply_markup=build_main_keyboard(),
    )


@bot.message_handler(func=lambda msg: msg.text == BTN_CURRENT_WEATHER_GEO)
def handle_current_weather_geo(message: types.Message) -> None:
    maybe_notification_check_on_update(message.from_user.id)
    pending_location_action[message.from_user.id] = "current_weather_geo"
    button = types.KeyboardButton("Отправить местоположение", request_location=True)
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.add(button)
    bot.send_message(
        message.chat.id,
        "Отправьте точку на карте (кнопка ниже), чтобы получить текущую погоду.",
        reply_markup=keyboard,
    )


@bot.message_handler(func=lambda msg: msg.text == BTN_MY_LOCATION)
def handle_save_my_location(message: types.Message) -> None:
    maybe_notification_check_on_update(message.from_user.id)
    pending_location_action[message.from_user.id] = "save_location"
    button = types.KeyboardButton("Отправить местоположение", request_location=True)
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.add(button)
    bot.send_message(
        message.chat.id,
        "Отправьте местоположение, чтобы сохранить координаты.",
        reply_markup=keyboard,
    )


@bot.message_handler(func=lambda msg: msg.text == BTN_FORECAST_5D)
def handle_five_day_forecast(message: types.Message) -> None:
    user_id = message.from_user.id
    maybe_notification_check_on_update(user_id)
    coords = user_locations.get(user_id)
    if not coords:
        bot.send_message(
            message.chat.id,
            f"Сначала сохраните геолокацию через «{BTN_MY_LOCATION}».",
        )
        return
    lat, lon = coords
    forecast = get_forecast_5d3h(lat, lon)
    if not forecast:
        bot.send_message(message.chat.id, "Не удалось получить прогноз.")
        return
    grouped = group_forecast_by_day(forecast)
    user_forecasts[user_id] = grouped
    persist_user(user_id)
    send_days_menu(message.chat.id, user_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("day:"))
def handle_day_details(call: types.CallbackQuery) -> None:
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    maybe_notification_check_on_update(user_id)
    day_key = call.data.split(":", 1)[1]
    days_data = user_forecasts.get(user_id, {})
    points = days_data.get(day_key)
    if not points:
        bot.send_message(call.message.chat.id, "Данные по этому дню не найдены.")
        return

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅ Назад к дням", callback_data="back:days"))
    safe_delete_inline(call.message.chat.id, user_id)
    sent = bot.send_message(
        call.message.chat.id,
        f"{summarize_day(day_key, points)}\n\n{detailed_day_text(day_key, points)}",
        reply_markup=markup,
    )
    last_inline_message_id[user_id] = sent.message_id
    persist_user(user_id)


@bot.callback_query_handler(func=lambda call: call.data == "back:days")
def handle_back_to_days(call: types.CallbackQuery) -> None:
    bot.answer_callback_query(call.id)
    maybe_notification_check_on_update(call.from_user.id)
    send_days_menu(call.message.chat.id, call.from_user.id)


@bot.message_handler(func=lambda msg: msg.text == BTN_NOTIFICATIONS)
def handle_notifications_button(message: types.Message) -> None:
    maybe_notification_check_on_update(message.from_user.id)
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(types.KeyboardButton("Да"), types.KeyboardButton("Нет"))
    prompt = bot.send_message(
        message.chat.id,
        "Включить уведомления? (Да/Нет)",
        reply_markup=markup,
    )
    bot.register_next_step_handler(prompt, process_notifications_enable)


def process_notifications_enable(message: types.Message) -> None:
    maybe_notification_check_on_update(message.from_user.id)
    choice = (message.text or "").strip().lower()
    user_id = message.from_user.id
    if choice == "да":
        notification_subscriptions[user_id] = True
        persist_user(user_id)
        prompt = bot.send_message(
            message.chat.id,
            "Введите интервал в часах (1–24). По умолчанию: 2.",
            reply_markup=build_main_keyboard(),
        )
        bot.register_next_step_handler(prompt, process_notifications_interval)
        return
    if choice == "нет":
        notification_subscriptions[user_id] = False
        persist_user(user_id)
        bot.send_message(
            message.chat.id,
            "Уведомления выключены.",
            reply_markup=build_main_keyboard(),
        )
        return
    bot.send_message(
        message.chat.id,
        "Введите только 'Да' или 'Нет'.",
        reply_markup=build_main_keyboard(),
    )


def process_notifications_interval(message: types.Message) -> None:
    maybe_notification_check_on_update(message.from_user.id)
    user_id = message.from_user.id
    raw = (message.text or "").strip()
    try:
        hours = int(raw)
    except ValueError:
        hours = DEFAULT_NOTIFY_INTERVAL_H
    hours = max(1, min(24, hours))
    notification_interval_hours[user_id] = hours
    notification_subscriptions[user_id] = True
    persist_user(user_id, {"notifications": {"interval_h": hours}})
    try_send_notification(user_id, force=True)
    bot.send_message(
        message.chat.id,
        f"Уведомления включены ✅. Интервал: {hours} ч.",
        reply_markup=build_main_keyboard(),
    )


@bot.message_handler(func=lambda msg: msg.text == BTN_COMPARE_CITIES)
def handle_compare_cities_button(message: types.Message) -> None:
    maybe_notification_check_on_update(message.from_user.id)
    prompt = bot.send_message(
        message.chat.id,
        "Введите 2 города через запятую.\nПример: Москва, Санкт-Петербург",
    )
    bot.register_next_step_handler(prompt, process_city_comparison)


def process_city_comparison(message: types.Message) -> None:
    maybe_notification_check_on_update(message.from_user.id)
    text = (message.text or "").strip()
    if "," not in text:
        bot.send_message(message.chat.id, "Нужны 2 города через запятую.")
        return
    city_a, city_b = [part.strip() for part in text.split(",", maxsplit=1)]
    if not city_a or not city_b:
        bot.send_message(message.chat.id, "Оба города должны быть заполнены.")
        return

    coord_a = get_coordinates(city_a)
    coord_b = get_coordinates(city_b)
    if not coord_a or not coord_b:
        bot.send_message(message.chat.id, "Один из городов не найден.")
        return

    weather_a = get_current_weather(coord_a[0], coord_a[1])
    weather_b = get_current_weather(coord_b[0], coord_b[1])
    if not weather_a or not weather_b:
        bot.send_message(message.chat.id, "Не удалось получить погоду для сравнения.")
        return

    temp_a = weather_a.get("main", {}).get("temp", "n/a")
    temp_b = weather_b.get("main", {}).get("temp", "n/a")

    response = (
        "🏙 <b>Сравнение городов</b>\n\n"
        "<pre>"
        "Показатель      | Город 1 | Город 2\n"
        "----------------|---------|--------\n"
        f"Название        | {city_a[:7]:7} | {city_b[:7]:7}\n"
        f"Температура °C  | {str(temp_a):7} | {str(temp_b):7}\n"
        "</pre>"
    )
    bot.send_message(message.chat.id, response)


@bot.message_handler(func=lambda msg: msg.text == BTN_EXTENDED)
def handle_extended_button(message: types.Message) -> None:
    maybe_notification_check_on_update(message.from_user.id)
    prompt = bot.send_message(
        message.chat.id,
        (
            "Введите город ИЛИ координаты через запятую (lat, lon).\n"
            "Примеры:\n"
            "• Казань\n"
            "• 55.7558, 37.6173"
        ),
    )
    bot.register_next_step_handler(prompt, process_extended_data)


@bot.inline_handler(func=lambda query: True)
def handle_inline_query(query: types.InlineQuery) -> None:
    city = (query.query or "").strip()
    if not city:
        return

    coords = get_coordinates(city)
    if not coords:
        return
    lat, lon = coords
    weather = get_current_weather(lat, lon)
    if not weather:
        return

    city_name = weather.get("name", city)
    temperature = weather.get("main", {}).get("temp", "n/a")
    description = weather.get("weather", [{}])[0].get("description", "без описания")
    forecast_link = build_forecast_link(city_name, lat, lon)

    message_text = (
        f"🌤 <b>{city_name}</b>\n"
        f"🌡 Температура: <b>{temperature}°C</b>\n"
        f"📝 Описание: {description.capitalize()}\n"
        f"🔗 <a href=\"{forecast_link}\">Ссылка на прогноз</a>"
    )

    result = types.InlineQueryResultArticle(
        id=f"weather_{city_name}_{lat:.3f}_{lon:.3f}",
        title=f"{city_name}: {temperature}°C",
        description=f"{description.capitalize()}",
        input_message_content=types.InputTextMessageContent(
            message_text=message_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        ),
    )
    bot.answer_inline_query(query.id, [result], cache_time=60, is_personal=True)


def process_extended_data(message: types.Message) -> None:
    text = (message.text or "").strip()
    lat: float
    lon: float
    if "," in text:
        left, right = [part.strip() for part in text.split(",", maxsplit=1)]
        try:
            lat = float(left)
            lon = float(right)
        except ValueError:
            bot.send_message(message.chat.id, "Координаты должны быть числами.")
            return
    else:
        coords = get_coordinates(text)
        if not coords:
            bot.send_message(message.chat.id, "Город не найден.")
            return
        lat, lon = coords

    weather = get_current_weather(lat, lon)
    if not weather:
        bot.send_message(message.chat.id, "Не удалось получить данные.")
        return
    user_locations[message.from_user.id] = (lat, lon)
    user_id = message.from_user.id
    if "," in text:
        persist_user(user_id, {"city": weather.get("name", f"{lat}, {lon}")})
    else:
        persist_user(user_id, {"city": weather.get("name", text)})
    bot.send_message(message.chat.id, format_extended_report(lat, lon, weather))


@bot.message_handler(func=lambda msg: True, content_types=["text"])
def fallback_text_handler(message: types.Message) -> None:
    text = (message.text or "").strip().lower()
    if text in {"да", "нет"}:
        return
    bot.send_message(
        message.chat.id,
        "Выберите действие через меню ниже 👇",
        reply_markup=build_main_keyboard(),
    )


if __name__ == "__main__":
    load_user_data()
    notifier = threading.Thread(target=notification_worker, daemon=True)
    notifier.start()
    print("Бот запущен... Нажмите Ctrl+C для остановки.")
    bot.infinity_polling(skip_pending=True)
