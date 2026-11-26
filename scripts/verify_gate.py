"""verify_gate.py — гейт приёмки артефактов пакета its-harness.

Вход: путь к файлу-артефакту.
Проверки: файл существует, не пустой, нет placeholder'ов, нет паттернов секретов.
Выход: PASS/FAIL (exit 0/1). При PASS — строка в memory/verification-log.md
(рядом с рабочей папкой; путь к логу = <cwd>/memory/verification-log.md).
PASS ставит только этот скрипт.

ПАТЧ (mavis-2026-11-11-003, внесён Alex в кит): добавлен режим --mode artifact|source.
  - artifact (по умолчанию, поведение v1 не меняется): полный набор placeholder'ов,
    включая угловые скобки. Для артефактов-поставок.
  - source: для исходников (скиллы, скрипты, документы ядра) — проверяется только аудит
    секретов. Плейсхолдеры в исходниках легальны: угловые скобки — это нотация документации
    ("<артефакт>"), а сами шаблоны маркеров физически присутствуют в коде гейтов, поэтому
    проверять их в файлах гейтов бессмысленно (гейт не проходит сам себя).
Причина: в режиме artifact гейт не проходит сам себя и общие инструменты
_инфра\bin (проверено 11.11: verify_gate.py, harness_checks_lib.py, redact-secrets.py,
token-usage-report.py — все дают FAIL на угловых скобках). Режимы по умолчанию
не меняются, обратная совместимость сохранена.

Usage: python verify_gate.py <файл> [--mode artifact|source]
"""
import os
import re
import sys
from datetime import date

ANGLE_PLACEHOLDER = r"<[^>]{2,}>"
TEXT_PLACEHOLDERS = [r"\bTODO\b", r"\bFIXME\b", r"\bTBD\b"]
LOOSE_PLACEHOLDERS = ["XXX"]
SECRETS = [r"sk-[A-Za-z0-9_-]{10,}", r"(?i)api[_-]?key\s*[:=]\s*\S+", r"(?i)password\s*[:=]\s*\S+",
           r"(?i)bearer\s+[A-Za-z0-9._-]{10,}"]


def check(path: str, mode: str = "artifact") -> list[str]:
    problems = []
    if not os.path.exists(path):
        return [f"файл не найден: {path}"]
    if os.path.getsize(path) == 0:
        return ["файл пустой"]
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except Exception as e:
        return [f"не читается как текст: {e}"]

    patterns = list(TEXT_PLACEHOLDERS) if mode == "artifact" else []
    if mode == "artifact":
        patterns.append(ANGLE_PLACEHOLDER)
        patterns.extend(LOOSE_PLACEHOLDERS)
    for p in patterns:
        m = re.findall(p, text)
        if m:
            problems.append(f"placeholder '{p}': {len(m)} шт. (напр. {m[0][:40]})")
    for p in SECRETS:
        if re.search(p, text):
            problems.append(f"похоже на секрет: паттерн '{p}'")
    return problems


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = [a for a in sys.argv[1:]]
    mode = "artifact"
    if "--mode" in args:
        i = args.index("--mode")
        try:
            mode = args[i + 1]
        except IndexError:
            print("Usage: python verify_gate.py <файл> [--mode artifact|source]")
            return 2
        del args[i:i + 2]
        if mode not in ("artifact", "source"):
            print("--mode: допустимо artifact или source")
            return 2
    if len(args) != 1:
        print("Usage: python verify_gate.py <файл> [--mode artifact|source]")
        return 2
    target = args[0]
    problems = check(target, mode)
    log = os.path.join(os.getcwd(), "memory", "verification-log.md")
    if problems:
        print(f"FAIL: {target}")
        for p in problems:
            print(f"  - {p}")
        return 1
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"- {date.today().isoformat()}: PASS {target}\n")
    print(f"PASS: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
