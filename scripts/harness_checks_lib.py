#!/usr/bin/env python3
r"""harness_checks_lib.py — общие инварианты статических гейтов.

Назначение: пересекающиеся проверки check-harness.py (Alex/Kimi) и
check-hermes-harness.py (Hermes) вынесены в общую библиотеку. Согласовано
в thread harness-review-2026-09 (R6): маппинг принят, draft — Hermes,
ревью и commit — Alex (2026-09-12).

Использование:
    sys.path.insert(0, r"C:\MyAI\_infra\bin")  # или скрипт лежит в bin/
    import harness_checks_lib as hcl
    status, name, detail = hcl.size_gate(path, "name", warn_at=150, fail_at=200)

Контракт:
  - Каждая проверка возвращает кортеж (status, name, detail),
    status ∈ {"PASS", "WARN", "FAIL"}.
  - Exit-логика (подсчёт errors/warnings, --strict, --json) остаётся
    в скриптах-владельцах.
  - Вся хост-специфика (пути, пороги) передаётся параметрами; в либе
    нет хардкода путей конкретного агента.

Зависимости: stdlib only; для mode="redact" — redact-secrets.py (путь параметром).

Владельцы проверок (кто что использует):
  size_gate          — Alex (строки AGENTS/INDEX/DECISIONS), Hermes (МБ state.db)
  files_exist        — Alex (ARCH-1 key_files), Hermes (config_files)
  check_md_links     — Alex (REF_RE rules/skills), Hermes (ссылки skills→_infra)
  secrets_audit      — Alex (redact-secrets.py по свежим .md), Hermes (.env placeholders)
  skills_frontmatter — оба (recursive=True покрывает оба layout'а)
  skills_duplication — оба

НЕ входит (остаётся у владельцев, хост-специфика):
  - ARCH-блок Alex (hooks alive, snapshot secrets, CRLF, harness_state)
  - orphan tool calls / todo (Hermes, proxy по state.db)
  - env-placeholder check Hermes — частный случай secrets_audit(paths, mode="placeholders")

История:
  0.1.2 (2026-09-12) — commit Alex. Фикс ревью: files_exist больше не
    возвращает «0 файлов» при paths-генераторе (приводится к list на входе).
  0.1.2-draft — Hermes: фикс фильтра точечных каталогов (f.relative_to(root).parts
    вместо f.parts — абсолютный путь C:\Users\x\.kimi-code\skills резал весь
    каталог); фикс _DESC_RE — YAML block-scalar ("description: |" / ">") не
    матчится как значение (ложные дубликаты).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

# Windows-консоль — принудительно UTF-8 (как в обоих скриптах-владельцах)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

__version__ = "0.1.2"

CheckResult = tuple  # (status, name, detail)


# ---------------------------------------------------------------------------
# 1. size_gate — лимиты размеров (строки или мегабайты)
# ---------------------------------------------------------------------------
def size_gate(path: Path, name: str, warn_at: float, fail_at: float,
              unit: str = "lines") -> CheckResult:
    """Лимит размера файла. unit: "lines" (Alex) | "mb" (Hermes state.db).

    Пороги: <= warn_at PASS, <= fail_at WARN, иначе FAIL.
    fail_at=float("inf") — FAIL никогда (только предупреждение).
    Отсутствующий файл → WARN (не у всех владельцев отсутствие = ошибка).
    """
    p = Path(path)
    if not p.is_file():
        return ("WARN", name, f"{p} отсутствует")
    if unit == "lines":
        value = sum(1 for _ in p.open(encoding="utf-8", errors="ignore"))
        fmt = f"{value} строк"
    elif unit == "mb":
        value = p.stat().st_size / (1024 * 1024)
        fmt = f"{value:.1f} МБ"
    else:
        raise ValueError(f"unit должен быть 'lines' или 'mb', получено {unit!r}")
    if value <= warn_at:
        return ("PASS", name, fmt)
    if value <= fail_at:
        return ("WARN", name, f"{fmt} (>{warn_at:g})")
    return ("FAIL", name, f"{fmt} (>{fail_at:g})")


# ---------------------------------------------------------------------------
# 2. files_exist — критичные файлы существуют и непусты
# ---------------------------------------------------------------------------
def files_exist(paths, name: str, require_nonempty: bool = True) -> CheckResult:
    """paths: итерируемое Path/str. Возвращает список отсутствующих в detail."""
    paths = list(paths)  # генератор иначе съедается циклом, total=0 (фикс 0.1.2)
    missing = []
    for raw in paths:
        p = Path(raw)
        if not p.exists() or (require_nonempty and p.is_file() and p.stat().st_size == 0):
            missing.append(str(p))
    if missing:
        return ("FAIL", name, f"отсутствуют/пусты: {', '.join(missing)}")
    return ("PASS", name, f"{len(paths)} ключевых файлов на месте")


# ---------------------------------------------------------------------------
# 3. check_md_links — живость относительных ссылок в markdown
# ---------------------------------------------------------------------------
def check_md_links(file: Path, base: Path, name: str,
                   pattern: str | re.Pattern = r"(?:rules|skills)[\\/][\w.\-\\/]+?\.md",
                   abs_prefix_strip: str | None = None) -> CheckResult:
    """Извлекает из текста ссылки по regex, проверяет существование от base.

    Если в pattern есть capture-группа, findall вернёт её (как REF_RE у Alex).
    abs_prefix_strip: опциональный regex-префикс абсолютного пути, который
    нужно отрезать (у Alex: r"C:[\\/]+Users[\\/]+\\w+[\\/]+\\.kimi-code[\\/]+").
    """
    p = Path(file)
    if not p.is_file():
        return ("FAIL", name, f"{p} отсутствует")
    text = p.read_text(encoding="utf-8", errors="ignore")
    pat = re.compile(pattern) if isinstance(pattern, str) else pattern
    refs = sorted({m.replace("\\", "/") for m in pat.findall(text)})
    if abs_prefix_strip:
        refs = [re.sub(abs_prefix_strip, "", r) for r in refs]
    broken = [r for r in refs if not (Path(base) / r).is_file()]
    if broken:
        return ("FAIL", name, f"битые ссылки: {', '.join(broken)}")
    return ("PASS", name, f"{len(refs)} ссылок валидны")


# ---------------------------------------------------------------------------
# 4. secrets_audit — аудит секретов (два режима)
# ---------------------------------------------------------------------------
def secrets_audit(paths, name: str, redact_script: Path | str | None = None,
                  mode: str = "redact",
                  placeholder_values=("placeholder", "<YOUR_KEY>",
                                      "CHANGE_ME", "TODO", "xxx")) -> CheckResult:
    """mode="redact"  — обёртка над redact-secrets.py --audit (канон Alex):
                         для каждого файла subprocess → JSON, total_findings>0 → FAIL.
       mode="placeholders" — проверка KEY=VALUE файлов (.env Hermes):
                         пустые/placeholder-значения → WARN.
    paths: итерируемое файлов. redact_script обязателен для mode="redact".
    """
    files = [Path(x) for x in paths if Path(x).is_file()]
    if mode == "redact":
        if redact_script is None:
            return ("FAIL", name, "mode=redact требует redact_script")
        findings = {}
        for f in files:
            r = subprocess.run(
                [sys.executable, str(redact_script), "--audit", str(f)],
                capture_output=True, text=True, encoding="utf-8", errors="ignore",
            )
            try:
                total = json.loads(r.stdout or "{}").get("total_findings", 0)
            except json.JSONDecodeError:
                total = 0
            if total:
                findings[f.name] = total
        if findings:
            return ("FAIL", name,
                    "; ".join(f"{k}: {v}" for k, v in findings.items()))
        return ("PASS", name, f"{len(files)} файлов чисты")
    if mode == "placeholders":
        empty = []
        checked = 0
        for f in files:
            for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                checked += 1
                if not value or value.lower() in tuple(v.lower() for v in placeholder_values):
                    empty.append(key.strip())
        if empty:
            return ("WARN", name, f"пустые placeholder'ы: {', '.join(empty[:5])}")
        return ("PASS", name, f"{checked} значений, все заполнены")
    raise ValueError(f"неизвестный mode {mode!r}")


# ---------------------------------------------------------------------------
# 4b. check_links — живость http(s)-ссылок в md/yaml (2026-11-11, hermes-002 §3.3a)
# ---------------------------------------------------------------------------
LINK_CACHE_DEFAULT = Path(r"C:\MyAI\_infra\state\link-check-cache.json")
LINK_CACHE_TTL_SEC = 24 * 3600


def _extract_urls(file: Path) -> list[str]:
    import re as _re
    text = file.read_text(encoding="utf-8", errors="ignore")
    return sorted(set(_re.findall(r"https?://[^\s\)\]\"'<>,]+", text)))


def check_links(paths, name: str = "links_alive",
                cache_file: Path | str | None = None,
                cache_ttl_sec: int = LINK_CACHE_TTL_SEC,
                timeout: int = 10, retries: int = 2) -> CheckResult:
    """Живость внешних http(s)-ссылок из md/yaml файлов.

    HEAD, fallback GET с Range: bytes=0-0. Классификация: 200/3xx — OK;
    403/429 — WARN (антибот ≠ смерть ссылки); 404/410/DNS-fail — FAIL.
    Кэш результатов на cache_ttl_sec (по умолчанию 24 ч), чтобы не долбить
    внешние хосты; кэш — JSON {url: {"ts": epoch, "cls": "ok|warn|fail"}}.
    Вердикт: FAIL если хотя бы одна fail-ссылка; WARN если есть warn без fail.
    """
    import time as _time
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError

    cache_path = Path(cache_file) if cache_file else LINK_CACHE_DEFAULT
    cache = {}
    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}

    urls = []
    for p in paths:
        f = Path(p)
        if f.is_file() and f.suffix.lower() in (".md", ".yaml", ".yml"):
            urls.extend((u, f.name) for u in _extract_urls(f))
    if not urls:
        return ("PASS", name, "ссылок не найдено")

    now = _time.time()
    fails, warns, checked, cached_hits = [], [], 0, 0

    def classify(url: str) -> str:
        for _ in range(retries):
            try:
                req = Request(url, method="HEAD",
                              headers={"User-Agent": "harness-link-check/1.0"})
                with urlopen(req, timeout=timeout) as r:
                    code = r.status
            except HTTPError as e:
                code = e.code
                if code == 405:  # HEAD не поддержан — GET с Range
                    try:
                        req = Request(url, headers={
                            "User-Agent": "harness-link-check/1.0",
                            "Range": "bytes=0-0"})
                        with urlopen(req, timeout=timeout) as r:
                            code = r.status
                    except HTTPError as e2:
                        code = e2.code
                    except URLError:
                        return "fail"
            except URLError:
                return "fail"
            except Exception:
                return "fail"
            if code == 429:
                continue  # retry
            break
        if code in (403, 429):
            return "warn"
        if code in (404, 410) or code is None:
            return "fail"
        return "ok" if code < 400 else "warn"

    for url, src in urls:
        ent = cache.get(url)
        if ent and now - ent.get("ts", 0) < cache_ttl_sec:
            cls = ent["cls"]
            cached_hits += 1
        else:
            cls = classify(url)
            if cls != "fail":  # fail не кэшируем: сбой сети ≠ мёртвая ссылка
                cache[url] = {"ts": now, "cls": cls}
            checked += 1
        if cls == "fail":
            fails.append(f"{url} ({src})")
        elif cls == "warn":
            warns.append(f"{url} ({src})")

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    except OSError:
        pass

    detail = (f"{len(urls)} ссылок ({checked} проверено, {cached_hits} из кэша)")
    if fails:
        return ("FAIL", name, f"{detail}; мёртвые: " + "; ".join(fails[:5]))
    if warns:
        return ("WARN", name, f"{detail}; антибот/429: " + "; ".join(warns[:5]))
    return ("PASS", name, detail + " — все живы")


# ---------------------------------------------------------------------------
# 5. skills_frontmatter — name/description в frontmatter каждого SKILL.md
# ---------------------------------------------------------------------------
NON_SKILL_DIRS = frozenset()  # владельцы передают свои (Alex: {"alex-context-templates"})


def skills_frontmatter(skills_dir: Path, name: str = "skills_frontmatter",
                       recursive: bool = True,
                       exclude_dirs: frozenset = NON_SKILL_DIRS,
                       required_keys=("name:", "description:")) -> CheckResult:
    """recursive=True (Hermes layout category/skill/SKILL.md) или
    False (плоский skills/<name>/SKILL.md). Служебные каталоги (точки) исключаются.

    NB: проверяет только найденные SKILL.md; каталог без SKILL.md не считается
    проблемой — при необходимости владелец добавляет свою проверку (у Alex это
    часть check_skills).
    """
    root = Path(skills_dir)
    if not root.is_dir():
        return ("WARN", name, f"{root} отсутствует")
    skill_files = root.rglob("SKILL.md") if recursive else root.glob("*/SKILL.md")
    # ВАЖНО: фильтруем только части ОТНОСИТЕЛЬНО root — абсолютный путь может
    # содержать точечные каталоги (например C:\Users\x\.kimi-code\skills),
    # наивный f.parts.startswith(".") вырежет весь каталог (баг draft v0.1.0).
    skill_files = [f for f in skill_files
                   if not any(part.startswith(".") for part in f.relative_to(root).parts)
                   and not (exclude_dirs & set(f.relative_to(root).parts))]
    problems, checked = [], 0
    for md in skill_files:
        checked += 1
        text = md.read_text(encoding="utf-8", errors="ignore")
        if not text.startswith("---"):
            problems.append(f"{md.parent.name}: нет frontmatter")
            continue
        fm = text.split("---", 2)[1] if text.count("---") >= 2 else ""
        missing = [k.rstrip(":") for k in required_keys if k not in fm]
        if missing:
            problems.append(f"{md.parent.name}: нет {', '.join(missing)}")
    if not problems:
        return ("PASS", name, f"{checked}/{checked} скиллов OK")
    sample = "; ".join(problems[:5])
    suffix = " …" if len(problems) > 5 else ""
    return ("FAIL", name, f"{len(problems)} проблем: {sample}{suffix}")


# ---------------------------------------------------------------------------
# 6. skills_duplication — одинаковые description у разных скиллов
# ---------------------------------------------------------------------------
# Не матчим YAML block-scalar ("description: |" / ">") — иначе все блочные
# описания ложно схлопываются в один дубликат. Первый символ захвата исключает
# whitespace: без этого \s* бэктрекает и матчит сам пробел перед "|" (баг v0.1.1).
_DESC_RE = re.compile(r"description:\s*['\"]?([^\s'\"\n|>][^'\"\n]*)['\"]?")


def skills_duplication(skills_dir: Path, name: str = "skills_duplication",
                       recursive: bool = True) -> CheckResult:
    root = Path(skills_dir)
    if not root.is_dir():
        return ("PASS", name, f"{root} отсутствует")
    skill_files = root.rglob("SKILL.md") if recursive else root.glob("*/SKILL.md")
    descriptions: dict[str, list[str]] = {}
    for md in skill_files:
        if any(part.startswith(".") for part in md.relative_to(root).parts):
            continue
        text = md.read_text(encoding="utf-8", errors="ignore")
        if not text.startswith("---"):
            continue
        m = _DESC_RE.search(text.split("---", 2)[1] if text.count("---") >= 2 else "")
        if m:
            desc = m.group(1).strip()
            descriptions.setdefault(desc, []).append(md.parent.name)
    dups = {d: s for d, s in descriptions.items() if len(s) > 1}
    if dups:
        sample = "; ".join(f"{d[:50]}... ({len(s)} скилла: {','.join(s[:3])})"
                           for d, s in list(dups.items())[:3])
        return ("WARN", name, f"{len(dups)} дублирующихся описаний: {sample}")
    return ("PASS", name, f"{len(descriptions)} уникальных описаний")
