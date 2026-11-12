"""Start a detached local booking worker under caffeinate."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from booking import MSK, ROOT, private_json, validate_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--at", required=True, help="YYYY-MM-DDTHH:MM:SS по Москве")
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    config_path = args.config.resolve()
    validate_config(json.loads(config_path.read_text()))
    start = datetime.strptime(args.at, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=MSK)
    if start <= datetime.now(MSK) or not 1 <= args.minutes <= 30:
        parser.error("Нужны будущее время и длительность от 1 до 30 минут")
    run = ROOT / "private" / datetime.now(MSK).strftime("run-%Y%m%d-%H%M%S-%f")
    run.parent.mkdir(exist_ok=True, mode=0o700)
    run.mkdir(mode=0o700)
    command = ["/usr/bin/caffeinate", "-i", sys.executable, "-u", str(ROOT / "booking.py"),
               "--config", str(config_path), "--at", args.at, "--minutes", str(args.minutes),
               "--headed", "--keep-open"]
    if args.commit:
        command.append("--commit")
    with os.fdopen(os.open(run / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        worker = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                  stdout=output, stderr=output, start_new_session=True)
    time.sleep(2)
    if worker.poll() is not None:
        raise RuntimeError(f"Процесс завершился при запуске; проверьте {run / 'worker.log'}")
    private_json(run / "process.json", {"pid": worker.pid, "at_msk": args.at, "commit": args.commit})
    print(f"Процесс запущен: PID {worker.pid}. Журнал: {run / 'worker.log'}")
    print("Компьютер должен оставаться включённым, с открытой крышкой и интернетом.")


if __name__ == "__main__":
    main()
