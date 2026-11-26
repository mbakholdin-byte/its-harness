r"""verify_acceptance.py — обязательный приёмочный шаг перед независимой проверкой (Mavis).

Смысл: независимому критику нельзя отдавать «на словах». Перед запуском верификатора
проверяющий обязан:
  1. прогнать канарейку гейтов (canary_gates.py) — гейты обязаны пропускать хорошее
     и отклонять плохое; иначе вердикт критика опирается на сломанные инструменты;
  2. прогнать verify_gate.py на артефакте (placeholder'ы, секреты, пустота);
  3. собрать пакет проверки по форме HANDOFF_PACKET: исходная задача (дословно),
     результат исполнителя, источники/требования, на которые опирался исполнитель.

Собранный пакет — это ровно те три input'а, которые канон VERIFIER_PASS §5 требует
отдать критику. Роль критика: `verifier` (свежий изолированный субагент, MiniMax Code).

Канон: VERIFIER_PASS v1.6 §5 и §9 (точка подключения Mavis — плагин its-harness).
Механика вызова: skills/agent-dispatch/SKILL.md §5; формат ответа: skills/verdict-file.

Использование:
  python verify_acceptance.py --artifact ФАЙЛ [--task ФАЙЛ_ИЛИ_ТЕКСТ] [--sources ФАЙЛ ...]
                              [--out БРИФ.md] [--skip-canary] [--json]

Коды возврата: 0 — пакет собран, всё прошло; 1 — артефакт не принят гейтом
(критику отдавать нечего); 2 — инфраструктурная ошибка (гейты/файлы не читаются).
"""
import argparse
import json
import os
import subprocess
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(PLUGIN_ROOT, "scripts")
MAX_CHARS = 20000


def _run(args, cwd=None):
    try:
        p = subprocess.run([sys.executable] + args, cwd=cwd, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=900)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:  # noqa: BLE001
        return -1, "запуск не удался: %s" % e


def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as e:
        return None


def build_brief(task_text, artifact_path, artifact_text, sources, checks):
    lines = ["# Пакет проверки (HANDOFF_PACKET)", "",
             "## goal", "",
             task_text.strip() or "(исходная задача не передана — критик обязан это указать)", "",
             "## sources", ""]
    if sources:
        for s in sources:
            lines.append("- %s" % s)
    else:
        lines.append("- (источники не указаны)")
    lines += ["", "## result", "",
              "Проверяемый артефакт: `%s`" % artifact_path, ""]
    if artifact_text is not None:
        body = artifact_text[:MAX_CHARS]
        if len(artifact_text) > MAX_CHARS:
            body += "\n\n[...обрезано, полный файл: %s]" % artifact_path
        lines += ["```markdown", body.rstrip(), "```", ""]
    else:
        lines += ["(не текстовый файл — проверяется по месту: %s)" % artifact_path, ""]
    lines += ["## constraints", "",
              "- Критик не предлагает альтернативное решение и не меняет scope задачи.",
              "- Вердикт — строго один из: PASS | FAIL | ESCALATE (первая строка файла вердикта).",
              "- Канарейка гейтов обязательна: если она не зелёная — вердикт ESCALATE "
              "(инструменты проверки неисправны, доверия к ним нет).", "",
              "## return", "",
              "- Путь к файлу вердикта `%s.verdict.md` рядом с артефактом.",
              "- Первая строка файла: `verdict: PASS|FAIL|ESCALATE`; секции «Проверял», "
              "«Не сошлось», «Не проверял».", "",
              "## checks_already_run", ""]
    for c in checks:
        lines.append("- %s" % c)
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Приёмочный шаг перед независимой проверкой")
    ap.add_argument("--artifact", required=True, help="проверяемый артефакт")
    ap.add_argument("--task", default=None, help="исходная задача: файл или текст")
    ap.add_argument("--sources", nargs="*", default=[], help="источники/требования")
    ap.add_argument("--out", default=None, help="куда записать бриф")
    ap.add_argument("--skip-canary", action="store_true",
                    help="пропустить канарейку (только для отладки; в проде нельзя)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    artifact = os.path.abspath(args.artifact)
    if not os.path.exists(artifact):
        print("артефакт не найден: %s" % artifact)
        return 2

    checks = []
    ok = True

    # 1. канарейка гейтов
    if not args.skip_canary:
        code, out = _run([os.path.join(SCRIPTS, "canary_gates.py"), "--strict"], cwd=PLUGIN_ROOT)
        last = out.strip().splitlines()[-1] if out.strip() else ""
        checks.append("канарейка гейтов: %s (exit %s)" % (last, code))
        if code != 0:
            ok = False
            print("канарейка не зелёная: %s" % last)
    else:
        checks.append("канарейка гейтов: ПРОПУЩЕНА (--skip-canary) — недопустимо для приёмки")

    # 2. verify_gate на артефакте
    code, out = _run([os.path.join(SCRIPTS, "verify_gate.py"), artifact], cwd=PLUGIN_ROOT)
    checks.append("verify_gate: %s (exit %s)" % (("PASS" if code == 0 else "FAIL"), code))
    if code != 0:
        ok = False
        print("verify_gate не принял артефакт:\n%s" % out)

    # 3. verdict_gate, если вердикт-файл уже есть рядом
    verdict = artifact[:-3] + ".verdict.md" if artifact.endswith(".md") else artifact + ".verdict.md"
    if os.path.exists(verdict):
        code, out = _run([os.path.join(SCRIPTS, "verdict_gate.py"), verdict], cwd=PLUGIN_ROOT)
        checks.append("verdict_gate: %s (exit %s)" % (("PASS" if code == 0 else "FAIL"), code))

    # 4. пакет проверки
    task_text = args.task or ""
    if task_text and os.path.exists(task_text):
        task_text = _read_text(task_text) or task_text
    artifact_text = _read_text(artifact) if os.path.isfile(artifact) else None
    brief = build_brief(task_text, artifact, artifact_text, args.sources, checks)

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(brief)
        print("бриф: %s" % args.out)

    if args.json:
        print(json.dumps({"artifact": artifact, "ok": ok, "checks": checks,
                          "brief_chars": len(brief)}, ensure_ascii=False, indent=2))
    else:
        print("ИТОГ: %s" % ("пакет собран, всё прошло" if ok else "есть блокирующие замечания"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
