"""Read authorized patient data locally; never echo it into tool output."""

import argparse
import json
from pathlib import Path
import urllib.request

from booking import ROOT, private_json, validate_config


def parse_patients(path):
    text = path.read_text()
    heading = "Для репетиции:" if "Для репетиции:" in text else "Пациент для репетиции:"
    blocks = text.split(heading)
    if len(blocks) != 2:
        raise ValueError("Нужны разделы основного пациента и пациента для репетиции")
    patients = []
    names = {"Фамилия": "lastName", "Имя": "firstName", "Дата рождения": "birthdate", "id врача": "doctor_id"}
    for block in blocks:
        patient = {"middleName": "", "email": "", "phone": ""}
        for line in block.splitlines():
            label, separator, value = line.partition(":")
            if separator and label.strip() in names:
                patient[names[label.strip()]] = value.strip()
        patients.append(patient)
    return patients


def make_config(patient, speciality, doctor_id):
    url = f"https://gorzdrav.spb.ru/_api/api/v2/schedule/lpu/172/speciality/{speciality}/doctors"
    with urllib.request.urlopen(url, timeout=20) as response:
        result = json.load(response)
    if result.get("success") is not True:
        raise RuntimeError("Не удалось получить список врачей")
    doctors = [d for d in result["result"] if str(d["id"]) == doctor_id]
    if len(doctors) != 1:
        raise RuntimeError("Врач не найден")
    return validate_config({"district": "8", "lpu": "172", "speciality": speciality,
                            "doctor_id": doctor_id, "doctor_name_contains": doctors[0]["name"],
                            "patient": patient})


def save_if_new(path, config):
    if path.exists():
        if json.loads(path.read_text()) != config:
            raise RuntimeError("Существующая конфигурация отличается; автоматическая перезапись отключена")
        return
    private_json(path, config, exclusive=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "client.md")
    parser.add_argument("--rehearsal-doctor", choices=["1562", "1918"])
    args = parser.parse_args()
    main_patient, rehearsal_patient = parse_patients(args.source)
    main_id = main_patient.pop("doctor_id", "839")
    rehearsal_id = rehearsal_patient.pop("doctor_id", args.rehearsal_doctor)
    if main_id != "839" or (rehearsal_id is not None and rehearsal_id not in ("1562", "1918")):
        raise ValueError("Неожиданный ID врача в файле")
    if args.rehearsal_doctor and rehearsal_id != args.rehearsal_doctor:
        raise ValueError("ID врача в файле и аргументе различаются")
    save_if_new(ROOT / "private" / "main.json", make_config(main_patient, "49", main_id))
    print("Основной пациент импортирован локально; врач 839, специальность 49.")
    if rehearsal_id:
        save_if_new(ROOT / "private" / "rehearsal.json", make_config(rehearsal_patient, "48", rehearsal_id))
        print(f"Пациент для репетиции импортирован локально; врач {rehearsal_id}, специальность 48.")
    print("Данные пациентов в вывод не включены. Запись не отправлялась.")


if __name__ == "__main__":
    main()
