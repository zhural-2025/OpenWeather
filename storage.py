import json
import os
from typing import Any

USER_DATA_FILE = "User_Data.json"


def _ensure_file() -> None:
    if not os.path.exists(USER_DATA_FILE):
        with open(USER_DATA_FILE, "w", encoding="utf-8") as file:
            json.dump({}, file, ensure_ascii=False, indent=2)


def _read_all() -> dict[str, Any]:
    _ensure_file()
    try:
        with open(USER_DATA_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_all(data: dict[str, Any]) -> None:
    _ensure_file()
    with open(USER_DATA_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def load_user(user_id: int) -> dict:
    data = _read_all()
    record = data.get(str(user_id), {})
    return record if isinstance(record, dict) else {}


def save_user(user_id: int, data: dict) -> None:
    all_data = _read_all()
    all_data[str(user_id)] = dict(data)
    _write_all(all_data)
