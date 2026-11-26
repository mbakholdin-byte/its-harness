#!/usr/bin/env python3
"""Гейт «жёсткого ядра» промпт-регресса.

Зачем: полный baseline помечает часть сценариев как flaky (их статус зависит от
настроения модели), поэтому «регрессов 0» в полном прогоне — следствие bless,
а не сигнал. Жёсткое ядро — отдельный набор сценариев, которые не падают на текущем
ядре: для них любой FAIL считается REGRESS и блокирует гейт.

Критерий отбора (2026-11-12, после ревизий набора сценариев Alex в 08:01, 08:31, 08:56 и 11:12): (а) ни
одного FAIL на текущем ядре (fp 99b660e0d5e132c5) при исправленных проверках; (б) в сценарии нет
контекстно-небезопасной блокирующей проверки — not_contains/not_regex, срабатывающей на
отрицательной или условной формулировке корректного ответа. Ядро — 7 сценариев:
`refuse-dangerous` (вернулся 12.11 после ревизии проверок владельцем — tempered-защита отрицаний и
модальности, пассивные формы «удаление выполнено/проведено»; независимый стенд защиты отрицаний
на его файле — 41/41), `no-tool-no-claim` (not_regex v3: отрицание в пределах предложения + формы
«передан/передана юристу»; 10/10 PASS на v2, 5/5 на v3), `handoff-return-contract` (флаг flaky
снят 11.11 — блокирует гейт). Исключён только `mail-lenkov` (пограничный), причины — в
`excluded` в baseline.

Защита отрицаний проверяется отдельным стендом `scripts/negation_battery.py` (41 кейс против
блокирующих проверок набора сценариев): он показывает ложные отказы и пропущенные нарушения
**до** прогона агента. 12.11 стенд нашёл второй класс дефектов в наборе (`refuse-dangerous`,
`anti-loop`); после правок владельца — 41/41.

Механика: отдельный baseline `evals/prompt_core_mavis.json` (baseline и ядро
те же, но flaky не проставлен) + прогон только по сценариям ядра. Код
`prompt_regress.py` не меняется: сценарий, которого нет в baseline, даёт
класс NEW (не регресс), а FAIL по сценарию с status=PASS и flaky=false даёт
REGRESS — ровно нужное поведение.

Использование:
    python scripts/prompt_core_gate.py            # прогон ядра
    python scripts/prompt_core_gate.py --dry-run  # без сетевых вызовов
    python scripts/prompt_core_gate.py --list      # показать состав ядра

Выход: 0 — ядро держится; 1 — есть регресс (блокирует).
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
CORE_BASELINE = os.path.join(PLUGIN, "evals", "prompt_core_mavis.json")
REGRESS = os.path.join(HERE, "prompt_regress.py")


def core_ids():
    with open(CORE_BASELINE, encoding="utf-8") as f:
        data = json.load(f)
    return list((data.get("scenarios") or {}).keys()), data


def main():
    ap = argparse.ArgumentParser(description="Гейт жёсткого ядра промпт-регресса")
    ap.add_argument("--dry-run", action="store_true", help="без сетевых вызовов")
    ap.add_argument("--list", action="store_true", help="показать состав ядра и выйти")
    ap.add_argument("--model", default=None, help="подопытная модель")
    ap.add_argument("--judge", default=None, help="модель судьи")
    args = ap.parse_args()

    ids, data = core_ids()
    if args.list:
        print("жёсткое ядро (%d сценариев), baseline %s" % (len(ids), CORE_BASELINE))
        for sid in ids:
            entry = (data.get("scenarios") or {}).get(sid, {})
            print("  %-28s baseline=%s flaky=%s" % (sid, entry.get("status"), entry.get("flaky")))
        return 0

    cmd = [sys.executable, REGRESS, "--baseline", CORE_BASELINE]
    for sid in ids:
        cmd += ["--scenario", sid]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.model:
        cmd += ["--model", args.model]
    if args.judge:
        cmd += ["--judge", args.judge]

    print("=== гейт жёсткого ядра ===")
    print("сценариев: %d (%s)" % (len(ids), ", ".join(ids)))
    sys.stdout.flush()
    res = subprocess.run(cmd)
    return res.returncode


if __name__ == "__main__":
    sys.exit(main())


