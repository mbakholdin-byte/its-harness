#!/usr/bin/env python3
"""Стенд защиты отрицаний и модальности для блокирующих проверок набора сценариев.

Зачем: детерминированные проверки (`not_contains` / `not_regex`) отвергают честные ответы,
если в них есть отрицание или модальность («Я **не** запускаю ещё раз», «Будет **безвозвратно
удалено**…»). Набор сценариев принадлежит владельцу, а гейт ядра ловит такие ложные отказы
только постфактум. Этот стенд проверяет проверки **до** прогона агента.

Как работает:
  * читает набор сценариев (по умолчанию — файл владельца, только на чтение);
  * для каждого сценария собирает блокирующие проверки (`not_contains`, `not_regex`);
  * прогоняет кейсы из `evals/negation_cases.json`:
      expect_violation=true  — проверка ДОЛЖНА сработать (настоящее нарушение);
      expect_violation=false — проверка НЕ должна срабатывать (корректная формулировка);
  * печатает расхождения (ложные отказы и пропущенные нарушения) и возвращает код:
      0 — все кейсы сошлись; 1 — есть расхождения; 2 — инфраструктурная ошибка.

Дополнительно `--audit`: печать всех блокирующих проверок с оценкой «есть ли в конструкции
защита отрицания/модальности» — эвристика для поиска кандидатов (кандидат = проверка, которую
стоит проверить батареей; решение — по батарее, не по эвристике).

Использование:
    python scripts/negation_battery.py                      # проверка набора владельца
    python scripts/negation_battery.py --scenarios <path>   # другой набор (например, после патча)
    python scripts/negation_battery.py --audit              # только аудит конструкций
    python scripts/negation_battery.py --json               # машинный вывод

Ключи: сетевых вызовов нет, ключи не нужны.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
DEFAULT_SCENARIOS = r"C:\MyAI\_infra\evals\harness\prompt_scenarios.json"
DEFAULT_CASES = os.path.join(PLUGIN, "evals", "negation_cases.json")

# эвристика «защиты» для --audit: lookbehind с отрицательным контекстом,
# tempered-запрет отрицания/модальности, явный запрет «не будет/не удалено»
GUARD = re.compile(r"\(\?<!|\(\?![^)]*\\b(?:не|нет|буд|никогд)|не\s+буд|только|лишь", re.I)


def load_scenarios(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    scenarios = raw.get("scenarios", []) if isinstance(raw, dict) else raw
    return scenarios


def blocking_checks(scenario):
    """[(значение, compiled|None)] — блокирующие проверки сценария."""
    out = []
    for chk in scenario.get("checks", []) or []:
        ctype = chk.get("type")
        if ctype == "not_regex":
            for v in chk.get("values", []) or []:
                try:
                    out.append((v, re.compile(v)))
                except re.error as e:
                    out.append((v, "BROKEN:%s" % e))
        elif ctype == "not_contains":
            for v in chk.get("values", []) or []:
                out.append((v, None))
    return out


def hits(checks, text):
    found = []
    for value, rx in checks:
        if isinstance(rx, str) and rx.startswith("BROKEN:"):
            found.append((value, "битый паттерн: %s" % rx[7:]))
        elif rx is None:
            if value.lower() in text.lower():
                found.append((value, "not_contains"))
        elif rx.search(text):
            found.append((value, "not_regex"))
    return found


def audit(scenarios):
    rows = []
    for sc in scenarios:
        for chk in sc.get("checks", []) or []:
            if chk.get("type") in ("not_regex", "not_contains"):
                for v in chk.get("values", []) or []:
                    rows.append((sc.get("id"), chk.get("type"), chk.get("label", ""), v,
                                 bool(GUARD.search(v))))
    print("блокирующих проверок всего: %d" % len(rows))
    unsafe = [r for r in rows if not r[4]]
    print("без защиты отрицания/модальности (кандидаты на проверку батареей): %d\n" % len(unsafe))
    for sid, ctype, label, val, _g in unsafe:
        print("[%s] %s" % (sid, ctype))
        print("   метка: %s" % (label or "—"))
        print("   паттерн: %s" % val[:200])
        print()
    return rows


def main():
    ap = argparse.ArgumentParser(description="Стенд защиты отрицаний для блокирующих проверок")
    ap.add_argument("--scenarios", default=DEFAULT_SCENARIOS, help="путь к набору сценариев")
    ap.add_argument("--cases", default=DEFAULT_CASES, help="путь к файлу кейсов")
    ap.add_argument("--audit", action="store_true", help="только аудит конструкций")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        scenarios = load_scenarios(args.scenarios)
    except Exception as e:  # noqa: BLE001
        print("ERR: не читается набор сценариев %s: %s" % (args.scenarios, e), file=sys.stderr)
        return 2
    if not scenarios:
        print("ERR: набор сценариев пуст", file=sys.stderr)
        return 2

    if args.audit:
        audit(scenarios)
        return 0

    try:
        with open(args.cases, encoding="utf-8") as f:
            cases = json.load(f).get("cases", [])
    except Exception as e:  # noqa: BLE001
        print("ERR: не читаются кейсы %s: %s" % (args.cases, e), file=sys.stderr)
        return 2

    by_id = {sc.get("id"): sc for sc in scenarios}
    results = []
    bad = []
    for c in cases:
        sid = c.get("scenario")
        sc = by_id.get(sid)
        if sc is None:
            bad.append({"case": c, "problem": "сценарий %r не найден в наборе" % sid})
            continue
        checks = blocking_checks(sc)
        if not checks:
            bad.append({"case": c, "problem": "у сценария нет блокирующих проверок"})
            continue
        found = hits(checks, c.get("text", ""))
        fact = bool(found)
        expected = bool(c.get("expect_violation"))
        ok = fact == expected
        results.append({"scenario": sid, "text": c.get("text", ""), "expected": expected,
                        "fact": fact, "ok": ok, "hit": found[0][0] if found else None,
                        "note": c.get("note", "")})
        if not ok:
            bad.append({"case": c, "fact": fact, "hit": found[0][0] if found else None})

    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    by_scenario = {}
    for r in results:
        s = by_scenario.setdefault(r["scenario"], {"total": 0, "ok": 0})
        s["total"] += 1
        s["ok"] += 1 if r["ok"] else 0

    print("=== стенд защиты отрицаний ===")
    print("набор сценариев: %s" % args.scenarios)
    print("кейсы: %s" % args.cases)
    print("")
    for sid in sorted(by_scenario):
        s = by_scenario[sid]
        print("%-24s %d/%d сошлись" % (sid, s["ok"], s["total"]))
    print("")
    for r in results:
        if not r["ok"]:
            kind = "ЛОЖНЫЙ ОТКАЗ" if r["fact"] else "ПРОПУЩЕННОЕ НАРУШЕНИЕ"
            print("  %-20s %s" % (kind, r["text"][:72]))
            print("      ожидалось срабатывание=%s, факт=%s, сработало: %s"
                  % (r["expected"], r["fact"], (r["hit"] or "—")[:90]))
            print("      кейс: %s" % r["note"])
    print("")
    print("ИТОГ: кейсов %d, сошлись %d, расхождений %d" % (total, passed, total - passed))

    if args.json:
        print(json.dumps({"total": total, "passed": passed, "results": results},
                         ensure_ascii=False, indent=2))

    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
