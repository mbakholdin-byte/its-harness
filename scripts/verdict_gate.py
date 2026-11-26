"""verdict_gate.py — гейт формата файла вердикта (MiniMax Code, its-harness).

Вход: путь к <артефакт>.verdict.md.
Проверки:
  - файл существует, не пустой, читается как текст;
  - ПЕРВАЯ строка: `verdict: PASS|FAIL|ESCALATE` (единственный источник истины для автоматики);
  - обязательные секции: «## Проверял», «## Не сошлось», «## Не проверял»;
  - при verdict: PASS секция «## Не сошлось» пуста или «—»;
  - нет placeholder'ов и нет паттернов секретов.
Выход: PASS/FAIL (exit 0/1). При PASS — строка в memory/verification-log.md
(путь к логу = <cwd>/memory/verification-log.md). PASS ставит только этот скрипт.

Канон: КАТАЛОГ_КАНОНОВ\\VERDICT_FILE.md v1.0

Usage: python verdict_gate.py <файл.verdict.md>
"""
import os
import re
import sys
from datetime import date

VERDICTS = ("PASS", "FAIL", "ESCALATE")
FIRST_LINE_RE = re.compile(r"^verdict:\s*(PASS|FAIL|ESCALATE)\s*$", re.IGNORECASE)
REQUIRED_SECTIONS = ("## Проверял", "## Не сошлось", "## Не проверял")
PLACEHOLDERS = [r"<[^>]{2,}>", r"\bTODO\b", r"\bFIXME\b", r"\bTBD\b", r"XXX"]
SECRETS = [r"sk-[A-Za-z0-9_-]{10,}", r"(?i)api[_-]?key\s*[:=]\s*\S+", r"(?i)password\s*[:=]\s*\S+",
           r"(?i)bearer\s+[A-Za-z0-9._-]{10,}"]


def _section_body(text: str, title: str) -> str:
    """Текст секции до следующего заголовка '## '."""
    start = text.find(title)
    if start < 0:
        return ""
    rest = text[start + len(title):]
    nxt = rest.find("\n## ")
    return rest if nxt < 0 else rest[:nxt]


def check(path: str) -> list[str]:
    problems = []
    if not os.path.exists(path):
        return [f"файл не найден: {path}"]
    if os.path.getsize(path) == 0:
        return ["файл пустой"]
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except Exception as e:
        return [f"не читается как текст: {e}"]

    first = text.lstrip("\ufeff").splitlines()[0] if text.splitlines() else ""
    m = FIRST_LINE_RE.match(first.strip())
    if not m:
        problems.append(
            "первая строка должна быть 'verdict: PASS|FAIL|ESCALATE' (сейчас: %r)" % first[:60]
        )
        verdict = ""
    else:
        verdict = m.group(1).upper()

    for section in REQUIRED_SECTIONS:
        if section not in text:
            problems.append(f"нет обязательной секции '{section}'")

    if verdict == "PASS":
        body = _section_body(text, "## Не сошлось").strip()
        if body and body not in ("-", "—", "нет", "нет замечаний"):
            problems.append("verdict: PASS, но секция «Не сошлось» не пуста")

    for p in PLACEHOLDERS:
        hits = re.findall(p, text)
        if hits:
            problems.append(f"placeholder '{p}': {len(hits)} шт. (напр. {hits[0][:40]})")
    for p in SECRETS:
        if re.search(p, text):
            problems.append(f"похоже на секрет: паттерн '{p}'")
    return problems


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) != 2:
        print("Usage: python verdict_gate.py <файл.verdict.md>")
        return 2
    target = sys.argv[1]
    problems = check(target)
    log = os.path.join(os.getcwd(), "memory", "verification-log.md")
    if problems:
        print(f"FAIL: {target}")
        for p in problems:
            print(f"  - {p}")
        return 1
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"- {date.today().isoformat()}: PASS verdict {target}\n")
    print(f"PASS: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
