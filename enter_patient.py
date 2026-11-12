"""Collect patient details locally, without printing them in terminal output."""

import argparse
from getpass import getpass
import json
from pathlib import Path

from booking import private_json, validate_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("private/main.json"))
    args = parser.parse_args()
    config = json.loads(Path(__file__).with_name("booking.example.json").read_text())
    print("Данные вводятся локально, символы не отображаются. Файл будет доступен владельцу.")
    for key, label in (
        ("lastName", "Фамилия"), ("firstName", "Имя"), ("middleName", "Отчество (если есть)"),
        ("birthdate", "Дата рождения ДД.ММ.ГГГГ"), ("email", "Почта (необязательно)"),
        ("phone", "Телефон (необязательно)"),
    ):
        config["patient"][key] = getpass(label + ": ").strip()
    validate_config(config)
    private_json(args.output, config, exclusive=True)
    print("Данные сохранены. Запись не отправлялась.")


if __name__ == "__main__":
    main()
