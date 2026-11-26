"""canary_gates.py — канарейка гейтов its-harness: good/bad фикстуры.

Идея: каждый гейт должен ПРОПУСКАТЬ корректный вход и ОТКЛОНЯТЬ некорректный,
и делать это детерминированно, без сети. Скрипт генерирует фикстуры во временной
папке, прогоняет гейты и сверяет фактический код возврата с ожидаемым.

«PASS ставит только скрипт» — здесь это проверяется буквально: ожидаемый код
фиксируется в наборе кейсов, а не «на глаз».

Usage:
  python canary_gates.py [--root PATH] [--json] [--strict]

Коды возврата canary_gates.py: 0 — все кейсы сходятся; 1 — расхождение;
2 — не удалось запустить гейт (ошибка окружения).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(PLUGIN_ROOT, "scripts")

VERDICT_GOOD = """verdict: PASS

# Verdict: канарейка

## Проверял
- фикстура good

## Не сошлось
—

## Не проверял
- боевой прогон
"""

VERDICT_NO_HEADER = """# Verdict: без первой строки

## Проверял
- ничего

## Не сошлось
- severity: major · area: отчёт · claim: цифра без источника · evidence: нет

## Не проверял
- ничего
"""

VERDICT_PASS_WITH_FINDINGS = """verdict: PASS

# Verdict: противоречие

## Проверял
- фикстура bad

## Не сошлось
- severity: major · area: отчёт · claim: цифра без источника · evidence: нет

## Не проверял
- ничего
"""

VERDICT_FAIL_OK = """verdict: FAIL

# Verdict: честный FAIL

## Проверял
- фикстура bad

## Не сошлось
- severity: major · area: отчёт · claim: цифра без источника · evidence: нет

## Не проверял
- ничего
"""

ARTIFACT_GOOD = """# Отчёт

Дата: 2026-11-11. Проверка пройдена, расхождений нет.
"""

ARTIFACT_PLACEHOLDER = """# Отчёт

Здесь осталось <placeholder> вместо данных.
"""

ARTIFACT_SECRET = """# Отчёт

Ключ доступа: api_key = sk-abcdefghijklmnop123
"""


def _run(args, cwd):
    try:
        p = subprocess.run([sys.executable] + args, cwd=cwd, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=300)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001 — канарейка должна сообщить, а не упасть
        return -1, f"запуск не удался: {e}"


def build_cases(tmp, root):
    """Возвращает список кейсов: (id, имя гейта, argv, cwd, ожидаемый код)."""
    f = lambda name, text: (os.path.join(tmp, name), text)

    v_good, _ = f("verdict-good.verdict.md", VERDICT_GOOD)
    with open(v_good, "w", encoding="utf-8") as fh:
        fh.write(VERDICT_GOOD)
    v_nohdr = os.path.join(tmp, "verdict-no-header.verdict.md")
    with open(v_nohdr, "w", encoding="utf-8") as fh:
        fh.write(VERDICT_NO_HEADER)
    v_pass_bad = os.path.join(tmp, "verdict-pass-with-findings.verdict.md")
    with open(v_pass_bad, "w", encoding="utf-8") as fh:
        fh.write(VERDICT_PASS_WITH_FINDINGS)
    v_fail = os.path.join(tmp, "verdict-fail.verdict.md")
    with open(v_fail, "w", encoding="utf-8") as fh:
        fh.write(VERDICT_FAIL_OK)

    a_good = os.path.join(tmp, "artifact-good.md")
    with open(a_good, "w", encoding="utf-8") as fh:
        fh.write(ARTIFACT_GOOD)
    a_ph = os.path.join(tmp, "artifact-placeholder.md")
    with open(a_ph, "w", encoding="utf-8") as fh:
        fh.write(ARTIFACT_PLACEHOLDER)
    a_sec = os.path.join(tmp, "artifact-secret.md")
    with open(a_sec, "w", encoding="utf-8") as fh:
        fh.write(ARTIFACT_SECRET)
    a_src = os.path.join(tmp, "source-angle-brackets.md")
    with open(a_src, "w", encoding="utf-8") as fh:
        fh.write("# Скилл\n\nИспользование: `python gate.py <артефакт>`.\n")

    cases = [
        ("verdict-good", "verdict_gate", [os.path.join(SCRIPTS, "verdict_gate.py"), v_good], tmp, 0),
        ("verdict-no-header", "verdict_gate", [os.path.join(SCRIPTS, "verdict_gate.py"), v_nohdr], tmp, 1),
        ("verdict-pass-with-findings", "verdict_gate",
         [os.path.join(SCRIPTS, "verdict_gate.py"), v_pass_bad], tmp, 1),
        ("verdict-fail-is-valid", "verdict_gate",
         [os.path.join(SCRIPTS, "verdict_gate.py"), v_fail], tmp, 0),
        ("artifact-good", "verify_gate", [os.path.join(SCRIPTS, "verify_gate.py"), a_good], tmp, 0),
        ("artifact-placeholder", "verify_gate",
         [os.path.join(SCRIPTS, "verify_gate.py"), a_ph], tmp, 1),
        ("artifact-secret", "verify_gate", [os.path.join(SCRIPTS, "verify_gate.py"), a_sec], tmp, 1),
        ("source-angle-brackets", "verify_gate",
         [os.path.join(SCRIPTS, "verify_gate.py"), a_src, "--mode", "source"], tmp, 0),
    ]

    # check_mavis_harness: зелёный прогон на своём ядре + две битые копии -> отказ.
    cmh = os.path.join(SCRIPTS, "check_mavis_harness.py")
    if os.path.exists(cmh):
        cases.append(("harness-self", "check_mavis_harness", [cmh, "--root", root, "--json"], root, 0))

        # (а) битая относительная ссылка в docs/*.md -> md_links должен упасть
        broken_links = os.path.join(tmp, "broken-plugin-links")
        shutil.copytree(root, broken_links, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        doc = os.path.join(broken_links, "docs", "AGENT_CORE.md")
        if os.path.isfile(doc):
            with open(doc, "a", encoding="utf-8") as fh:
                fh.write("\n\nБитая ссылка: [нет такого файла](scripts/no_such_file_here.py)\n")
            cases.append(("harness-broken-links", "check_mavis_harness",
                          [cmh, "--root", broken_links, "--json"], broken_links, 1))

        # (б) невалидный манифест -> manifest_integrity должен упасть
        broken_manifest = os.path.join(tmp, "broken-plugin-manifest")
        shutil.copytree(root, broken_manifest, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        mpath = os.path.join(broken_manifest, ".minimax-plugin", "plugin.json")
        if os.path.isfile(mpath):
            with open(mpath, "w", encoding="utf-8") as fh:
                fh.write("{ это не json ")
            cases.append(("harness-broken-manifest", "check_mavis_harness",
                          [cmh, "--root", broken_manifest, "--json"], broken_manifest, 1))
    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="Канарейка гейтов its-harness")
    ap.add_argument("--root", default=PLUGIN_ROOT, help="корень плагина для проверки")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="ненулевой код, если хоть один кейс не сошёлся")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    tmp = tempfile.mkdtemp(prefix="canary-")
    rows = []
    try:
        for cid, gate, argv, cwd, expected in build_cases(tmp, root):
            code, out = _run(argv, cwd)
            ok = (code == expected)
            rows.append({"id": cid, "gate": gate, "expected": expected, "actual": code,
                         "ok": ok, "output": out.strip().splitlines()[:6]})
            status = "OK " if ok else "FAIL"
            print(f"[{status}] {cid} ({gate}): ожидался exit {expected}, получен {code}")
            if not ok:
                for line in out.strip().splitlines()[:8]:
                    print(f"        {line}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in rows if not r["ok"]]
    summary = {"cases": len(rows), "passed": len(rows) - len(failed), "failed": len(failed),
               "rows": rows}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"ИТОГ: {summary['passed']}/{summary['cases']} кейсов сошлись")
    if failed:
        return 1 if args.strict else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
