# OpenWeather Case

Telegram-бот для погоды на базе `pyTelegramBotAPI` и OpenWeather API.

## Структура проекта

- `bot.py`
- `weather_app.py`
- `storage.py`
- `requirements.txt`
- `.gitignore`
- `.env.example`
- `User_Data.json`
- `README.md`

## Настройка

1. Создайте и активируйте виртуальное окружение.
2. Установите зависимости:

```powershell
python -m pip install -r requirements.txt
```

3. Создайте `.env` на основе `.env.example` и заполните переменные:

```env
OW_API_KEY=your_openweather_key
BOT_TOKEN=your_telegram_token
```

## Запуск

```powershell
python bot.py
```
