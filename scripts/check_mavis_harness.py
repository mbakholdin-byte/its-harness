#!/usr/bin/env python3
r"""check_mavis_harness.py — статический гейт харнеса агента Mavis (MiniMax Code).

Назначение: проверяет целостность плагина its-harness (skills, hooks, манифест,
docs, scripts, вердикт-файлы) и конфиг рантайма Mavis. Ничего не чинит и не
меняет — только читает (кроме кэша внешних ссылок в MAVIS_STATE_DIR).

Канон: скрипт владеет проверками плагина; общие инварианты берутся из
scripts/harness_checks_lib.py (read-only копия из MyAI, версия 0.1.2).

Использование:
    python check_mavis_harness.py                 # текстовый отчёт
    python check_mavis_harness.py --json          # машиночитаемый отчёт
    python check_mavis_harness.py --strict        # WARN считается провалом
    python check_mavis_harness.py --links         # + внешняя живость ссылок
    python check_mavis_harness.py --root PATH     # другой корень плагина

Переменные окружения:
    MAVIS_PLUGIN_ROOT  корень плагина (дефолт — каталог уровнем выше scripts/)
    MAVIS_HOME         корень рантайма (дефолт ~/.minimax) — ищется config.yaml
    MAVIS_STATE_DIR    каталог состояния (дефолт MAVIS_HOME/state)

Коды выхода:
    0 — все проверки PASS/WARN (без --strict)
    1 — есть FAIL (или есть WARN при --strict)
    2 — ошибка запуска (плохие пути, нечитаемый скрипт/библиотека)

Зависимости: stdlib only.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import traceback
from pathlib import Path

# Windows-консоль — UTF-8 (как в остальных скриптах плагина)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

__version__ = "0.1.0"

# Гейт не должен оставлять мусор в дереве плагина (никаких __pycache__)
sys.dont_write_bytecode = True

# ---------------------------------------------------------------------------
# Пороги (задаются в КБ; size_gate оперирует МБ — переводим)
# ---------------------------------------------------------------------------
DOC_WARN_KB = 12.0
DOC_FAIL_KB = 20.0
SCRIPT_WARN_KB = 40.0
SCRIPT_FAIL_KB = 80.0

# Обязательные файлы плагина
REQUIRED_FILES = (
    ".minimax-plugin/plugin.json",
    "hooks/hooks.json",
    "its-harness.mcp.json",
    "README.md",
    "docs/AGENT_CORE.md",
    "docs/TRUTH_SOURCES.md",
    "docs/RED_LINES.md",
    "scripts/verify_gate.py",
    "scripts/verdict_gate.py",
    "scripts/redteam_run.py",
)

# Ссылочный паттерн для check_md_links: относительные пути внутри плагина.
# Без capture-группы — findall отдаёт полное совпадение (контракт библиотеки).
PLUGIN_REL_LINK_RE = r"(?:docs|scripts|skills|hooks)[\\/][\w.\-\\/]+?\.(?:md|py|json)"

VERDICT_FIRST_LINE_RE = re.compile(r"^verdict:\s*(PASS|FAIL|ESCALATE)\s*$", re.IGNORECASE)

STATUS_ORDER = {"PASS": 0, "WARN": 1, "FAIL": 2}


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------
def _worst(results):
    """Схлопывает список (status, name, detail) в худший статус + сводный detail."""
    if not results:
        return ("WARN", "—", "нет файлов для проверки")
    worst = max((r[0] for r in results), key=lambda s: STATUS_ORDER.get(s, 1))
    parts = [f"{r[1]}: {r[2]}" for r in results]
    return (worst, results[0][1], "; ".join(parts))


def _load_lib():
    """Загружает harness_checks_lib.py рядом со скриптом (без sys.path pollution)."""
    lib_path = Path(__file__).resolve().parent / "harness_checks_lib.py"
    if not lib_path.is_file():
        raise FileNotFoundError(f"не найдена библиотека проверок: {lib_path}")
    spec = importlib.util.spec_from_file_location("harness_checks_lib", lib_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"не удалось загрузить {lib_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_json(path: Path):
    """(ok, data, error_text) — парсинг JSON без исключений наружу."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return (False, None, f"не читается: {e}")
    try:
        return (True, json.loads(text), None)
    except json.JSONDecodeError as e:
        return (False, None, f"JSON-ошибка: {e}")


# ---------------------------------------------------------------------------
# Проверки
# ---------------------------------------------------------------------------
def check_files_exist(hcl, root: Path):
    paths = [root / rel for rel in REQUIRED_FILES]
    return hcl.files_exist(paths, "files_exist", require_nonempty=True)


def check_manifest_integrity(root: Path):
    """Манифест/хуки/mcp: парсятся, skills-пути существуют, есть SessionStart/Stop."""
    name = "manifest_integrity"
    problems = []
    details = []

    manifest = root / ".minimax-plugin" / "plugin.json"
    ok, data, err = _read_json(manifest)
    if not ok:
        return ("FAIL", name, f"plugin.json невалиден: {err}")
    if not isinstance(data, dict):
        return ("FAIL", name, "plugin.json: ожидался объект")
    details.append(f"plugin.json v{data.get('version', '?')}")

    skills = data.get("skills")
    if not isinstance(skills, list) or not skills:
        problems.append("plugin.json: нет непустого списка 'skills'")
        skills = []
    missing = []
    for rel in skills:
        if not isinstance(rel, str):
            problems.append(f"skills: не строка ({rel!r})")
            continue
        if not (root / rel).is_file():
            missing.append(rel)
    if missing:
        problems.append("skills: не найдены " + ", ".join(missing))
    else:
        details.append(f"skills: {len(skills)} путей на месте")

    # скиллы на диске, не объявленные в манифесте
    skills_root = root / "skills"
    if skills_root.is_dir():
        declared = {Path(s).parent.name for s in skills if isinstance(s, str)}
        on_disk = {d.name for d in skills_root.iterdir()
                   if d.is_dir() and (d / "SKILL.md").is_file()}
        undeclared = sorted(on_disk - declared)
        if undeclared:
            problems.append("skills: не объявлены в манифесте: " + ", ".join(undeclared))

    hooks_path = root / "hooks" / "hooks.json"
    ok, hooks_data, err = _read_json(hooks_path)
    if not ok:
        problems.append(f"hooks.json невалиден: {err}")
    elif not isinstance(hooks_data, dict):
        problems.append("hooks.json: ожидался объект")
    else:
        hooks_map = hooks_data.get("hooks")
        if not isinstance(hooks_map, dict):
            hooks_map = hooks_data
        wanted = ("SessionStart", "Stop")
        absent = [h for h in wanted if h not in hooks_map]
        if absent:
            problems.append("hooks.json: нет событий " + ", ".join(absent))
        else:
            details.append("hooks: SessionStart/Stop")

    mcp_path = root / "its-harness.mcp.json"
    ok, mcp_data, err = _read_json(mcp_path)
    if not ok:
        problems.append(f"its-harness.mcp.json невалиден: {err}")
    elif not isinstance(mcp_data, dict) or "mcpServers" not in mcp_data:
        problems.append("its-harness.mcp.json: нет ключа 'mcpServers'")
    else:
        details.append("mcp.json: ok")

    if problems:
        return ("FAIL", name, "; ".join(problems))
    return ("PASS", name, "; ".join(details))


def check_skills(hcl, root: Path):
    """Фронтматтер и дубли описаний — через библиотеку (как есть)."""
    skills_dir = root / "skills"
    fm = hcl.skills_frontmatter(skills_dir, "skills_frontmatter", recursive=True)
    dup = hcl.skills_duplication(skills_dir, "skills_duplication", recursive=True)
    return [fm, dup]


def check_sizes(hcl, root: Path):
    """size_gate по docs/*.md и scripts/*.py (пороги в КБ → size_gate в МБ)."""
    results = []
    groups = (
        ("size_docs", sorted((root / "docs").glob("*.md")), DOC_WARN_KB, DOC_FAIL_KB),
        ("size_scripts", sorted((root / "scripts").glob("*.py")),
         SCRIPT_WARN_KB, SCRIPT_FAIL_KB),
    )
    for check_name, files, warn_kb, fail_kb in groups:
        if not files:
            results.append(("WARN", check_name, "нет файлов для проверки размера"))
            continue
        per_file = []
        for f in files:
            kb = f.stat().st_size / 1024.0
            status, _, detail = hcl.size_gate(
                f, f"{check_name}:{f.name}",
                warn_at=warn_kb / 1024.0, fail_at=fail_kb / 1024.0, unit="mb")
            # size_gate оперирует МБ — переписываем detail в КБ (пороги те же)
            per_file.append((status, f"{check_name}:{f.name}",
                             f"{kb:.1f} KB (лимит {warn_kb:g}/{fail_kb:g} KB)"))
        worst, _, detail = _worst(per_file)
        results.append((worst, check_name,
                        detail if worst != "PASS" else
                        f"{len(files)} файлов в лимите {warn_kb:g}/{fail_kb:g} KB"))
    return results


def check_secrets(hcl, root: Path):
    """Аудит секретов по docs + scripts + skills через redact-secrets.py."""
    redact = root / "scripts" / "redact-secrets.py"
    targets = []
    for sub in ("docs", "scripts", "skills"):
        d = root / sub
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            if f.is_file() and f.suffix.lower() in (".md", ".py", ".json", ".yaml", ".yml"):
                targets.append(f)
    if not redact.is_file():
        return ("FAIL", "secrets_audit", f"нет redact-скрипта: {redact}")
    if not targets:
        return ("WARN", "secrets_audit", "нет файлов для аудита")
    status, name, detail = hcl.secrets_audit(
        targets, "secrets_audit", redact_script=redact, mode="redact")
    return (status, name, detail)


def check_md_links(hcl, root: Path):
    """Битые относительные ссылки в docs/*.md (база — корень плагина)."""
    docs = sorted((root / "docs").glob("*.md"))
    if not docs:
        return ("WARN", "md_links", "нет docs/*.md")
    per_file = []
    for f in docs:
        per_file.append(hcl.check_md_links(
            f, root, f"md_links:{f.name}", pattern=PLUGIN_REL_LINK_RE))
    worst, _, detail = _worst(per_file)
    if worst == "PASS":
        return ("PASS", "md_links",
                "; ".join(r[2] for r in per_file))
    return (worst, "md_links", detail)


def _refine_link_result(status, name, detail):
    """Разделяет «мёртвая ссылка» (404/410 → FAIL) и «сеть недоступна» (→ WARN).

    check_links из библиотеки считает сетевой сбой (URLError) фатальным; по
    контракту гейта сетевые сбои — WARN, а FAIL — только 404/410.
    """
    if status != "FAIL":
        return (status, name, detail)
    urls = []
    for chunk in detail.split("мёртвые:")[-1].split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        url = chunk.split(" (")[0].strip()
        if url.startswith("http"):
            urls.append(url)
    if not urls:
        return ("FAIL", name, detail)
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen
    dead, net, other = [], [], []
    for url in urls:
        code = None
        try:
            req = Request(url, method="HEAD",
                          headers={"User-Agent": "mavis-harness-gate/1.0"})
            with urlopen(req, timeout=10) as r:
                code = r.status
        except HTTPError as e:
            code = e.code
        except (URLError, OSError):
            code = None
        if code in (404, 410):
            dead.append(url)
        elif code is None:
            net.append(url)
        else:
            # 403/429/5xx и прочие — не «мёртвая ссылка»
            other.append(f"{url} ({code})")
    if dead:
        return ("FAIL", name, f"мёртвые ссылки (404/410): {'; '.join(dead[:5])}")
    if net:
        return ("WARN", name,
                f"сеть недоступна для {len(net)} ссылок (сбой сети ≠ мёртвая ссылка)")
    return ("WARN", name, f"иные коды ответа: {'; '.join(other[:5])}")


def check_links(hcl, root: Path, state_dir: Path):
    """Внешняя живость http(s)-ссылок в docs/*.md (опционально, флаг --links)."""
    docs = sorted((root / "docs").glob("*.md"))
    if not docs:
        return ("WARN", "links_alive", "нет docs/*.md")
    cache = state_dir / "link-check-cache.json"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    status, name, detail = hcl.check_links(docs, name="links_alive", cache_file=cache)
    return _refine_link_result(status, name, detail)


def check_config_sanity(mavis_home: Path):
    """config.yaml: defaultModel, секция memory с enabled: true, permissionMode."""
    name = "config_sanity"
    cfg = mavis_home / "config.yaml"
    if not cfg.is_file():
        return ("FAIL", name, f"config.yaml не найден: {cfg}")
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ("FAIL", name, f"config.yaml не читается: {e}")

    problems = []
    details = []

    m = re.search(r"^defaultModel:\s*(\S+)\s*$", text, re.MULTILINE)
    if not m:
        problems.append("нет defaultModel")
    else:
        details.append(f"defaultModel={m.group(1)}")

    if not re.search(r"^memory:\s*$", text, re.MULTILINE):
        problems.append("нет секции memory:")
    else:
        block = re.search(r"^memory:\s*\n((?:[ \t]+.*\n?)*)", text, re.MULTILINE)
        block_text = block.group(1) if block else ""
        if not re.search(r"^\s+enabled:\s*true\s*$", block_text, re.MULTILINE):
            problems.append("memory.enabled != true")
        else:
            details.append("memory.enabled=true")

    if not re.search(r"^permissionMode:\s*\S+", text, re.MULTILINE):
        problems.append("нет ключа permissionMode")
    else:
        pm = re.search(r"^permissionMode:\s*(\S+)", text, re.MULTILINE)
        details.append(f"permissionMode={pm.group(1)}")

    if problems:
        return ("FAIL", name, "; ".join(problems))
    return ("PASS", name, "; ".join(details))


def check_verdict_files(root: Path):
    """Формат вердикт-файлов: первая строка `verdict: PASS|FAIL|ESCALATE`."""
    name = "verdict_files"
    files = sorted(list(root.glob("*.verdict.md")) + list((root / "docs").glob("*.verdict.md")))
    if not files:
        return ("WARN", name, "вердикт-файлов нет")
    problems = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            problems.append(f"{f.name}: не читается ({e})")
            continue
        lines = text.lstrip("\ufeff").splitlines()
        first = lines[0].strip() if lines else ""
        if not VERDICT_FIRST_LINE_RE.match(first):
            problems.append(f"{f.name}: первая строка {first[:40]!r}")
    if problems:
        return ("FAIL", name, "; ".join(problems[:5]))
    return ("PASS", name, f"{len(files)} файлов, формат первой строки корректен")


# ---------------------------------------------------------------------------
# Оркестрация
# ---------------------------------------------------------------------------
def run_checks(root: Path, mavis_home: Path, state_dir: Path, links: bool):
    hcl = _load_lib()
    results = []

    results.append(check_files_exist(hcl, root))
    results.append(check_manifest_integrity(root))
    results.extend(check_skills(hcl, root))
    results.extend(check_sizes(hcl, root))
    results.append(check_secrets(hcl, root))
    results.append(check_md_links(hcl, root))
    if links:
        results.append(check_links(hcl, root, state_dir))
    results.append(check_config_sanity(mavis_home))
    results.append(check_verdict_files(root))
    return results


def _summary(results, strict: bool):
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for status, _, _ in results:
        counts[status] = counts.get(status, 0) + 1
    ok = counts["FAIL"] == 0 and (not strict or counts["WARN"] == 0)
    overall = "PASS" if ok else "FAIL"
    return {
        "total": len(results),
        "pass": counts["PASS"],
        "warn": counts["WARN"],
        "fail": counts["FAIL"],
        "strict": strict,
        "status": overall,
        "ok": ok,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Статический гейт харнеса агента Mavis (плагин its-harness).")
    parser.add_argument("--json", action="store_true", help="машиночитаемый отчёт")
    parser.add_argument("--strict", action="store_true", help="WARN считается провалом")
    parser.add_argument("--links", action="store_true",
                        help="включить внешнюю проверку живости ссылок (сеть)")
    parser.add_argument("--root", default=None,
                        help="корень плагина (по умолчанию MAVIS_PLUGIN_ROOT)")
    args = parser.parse_args(argv)

    script_dir = Path(__file__).resolve().parent
    default_root = script_dir.parent

    root = Path(args.root).resolve() if args.root else Path(
        os.environ.get("MAVIS_PLUGIN_ROOT", str(default_root))).resolve()
    mavis_home = Path(os.environ.get("MAVIS_HOME", str(Path.home() / ".minimax"))).resolve()
    state_dir = Path(os.environ.get("MAVIS_STATE_DIR", str(mavis_home / "state"))).resolve()

    if not root.is_dir():
        print(f"ОШИБКА: корень плагина не найден: {root}", file=sys.stderr)
        return 2

    try:
        results = run_checks(root, mavis_home, state_dir, args.links)
    except Exception as e:  # noqa: BLE001 — ошибка запуска → код 2
        print(f"ОШИБКА: гейт не отработал: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    summary = _summary(results, args.strict)
    checks = [{"name": n, "status": s, "detail": d} for s, n, d in results]

    if args.json:
        print(json.dumps({
            "ok": summary["ok"],
            "root": str(root),
            "checks": checks,
            "summary": summary,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"== its-harness gate :: {root} ==")
        for c in checks:
            print(f"{c['status']:4}  {c['name']:28}  {c['detail']}")
        print("ИТОГ: {status} (всего {total}: PASS {pass}, WARN {warn}, FAIL {fail})".format(
            **summary))

    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
