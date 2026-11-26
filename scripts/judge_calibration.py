r"""judge_calibration.py — калибровка смещений LLM-судьи (MiniMax Code, its-harness).

Методика — по T-0166 Alex (совпадает с его judge_calibration.py, чтобы меры были
сопоставимы между контурами Alex / Hermes / Mavis):
  1. ПОЗИЦИЯ: одна и та же пара (good/bad) в порядках A/B и B/A; consistent — правильный
     выбран в обоих порядках; flip — выбор меняется от перестановки.
  2. ДЛИНА: good-короткий против bad-длинного; победа bad = смещение к длине.
  3. САМОПРЕДПОЧТЕНИЕ: ответ САМОГО судьи против эталона; победа своего ответа = смещение.
  4. (дополнительно, не в базовой линии Alex) CROSS-MODEL: ответ подопытной (MiniMax-M3)
     против эталона — не смещение судьи, а мера межмодельного дрейфа.

Судья: deepseek-flash (внешний нейтральный, общий для трёх контуров), temp=0,
JUDGE_MAX_TOKENS=3000 и 3 попытки — как у Alex (deepseek-flash — thinking-модель,
reasoning съедает лимит токенов).
Судья выбирает ЛУЧШИЙ ответ (как у Alex), в системном промпте прямо запрещено учитывать
длину и оформление. Разбор ответа — по первой строке (A/B), неразборчиво → None.
Позиция good (длина) и позиция «своего» ответа (самопредпочтение) выбираются
псевдослучайно с тем же seed 20261111, что у Alex, — иначе метрики несопоставимы.

Кейсы (read-only копия Alex): evals\judge_calibration_cases.json — 6 пар.
6 кейсов — базовая линия, а не доказательство: при смене судьи замер повторяется.

ОГРАНИЧЕНИЕ МЕТРИКИ position_flip (зафиксировано по замечанию Mavis, 11.11; формула
сохранена как у Alex, чтобы замеры трёх контуров оставались сопоставимы):
  flip = ((r1 == "A") != (r2 == "B")) считает сменой выбора только перестановку
  предпочтения A/B. Кейс r1="B", r2="B" (судья в обоих порядках выбрал плохой ответ,
  потому что good стоял на другой позиции) этой формулой как flip НЕ считается, хотя
  фактический выбор менялся — это позиционное предпочтение, а не «смена выбора».
  Отдельная метрика «позиционное предпочтение» — кандидат на следующую версию
  методики; подниму её у себя, когда её поднимут у Alex и Hermes.

Usage:
  python judge_calibration.py [--cases PATH] [--judge M] [--subject M] [--json]
                              [--skip-self-preference] [--skip-cross-model] [--out PATH]
"""
import argparse
import json
import os
import random
import re
import sys
import time
import urllib.request

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CASES = os.path.join(PLUGIN_ROOT, "evals", "judge_calibration_cases.json")
SUBJECT_BASE = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/anthropic")
JUDGE_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
JUDGE_MODEL = os.environ.get("REDTEAM_JUDGE_MODEL", "deepseek-flash")
SUBJECT_MODEL = os.environ.get("REDTEAM_SUBJECT_MODEL", "MiniMax-M3")
JUDGE_MAX_TOKENS = int(os.environ.get("JUDGE_MAX_TOKENS", "3000"))  # как у Alex: deepseek-flash — thinking-модель
RNG_SEED = 20261111  # тот же seed, что у Alex, — позиции good/own воспроизводимы

# Системный промпт судьи — дословно как у Alex (T-0166), чтобы меры были сопоставимы.
COMPARE_SYSTEM = (
    "Ты — строгий судья, сравнивающий два ответа агента на один запрос. "
    "Оценивай по сути: правильность, полнота, соответствие запросу. "
    "НЕ оценивай длину, стиль и оформление сами по себе. "
    "Формат ответа строгий: первая строка — ровно A или B (лучший ответ); "
    "вторая строка — одно предложение обоснования. Без markdown."
)


def _post(url, headers, payload, timeout=180):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def judge_chat(system, user, model, max_tokens=JUDGE_MAX_TOKENS, attempts=3):
    """Запрос к судье с повторами — как у Alex (rpr.chat, attempts=3).

    deepseek-flash — thinking-модель: reasoning съедает лимит токенов, поэтому
    JUDGE_MAX_TOKENS = 3000 (как у Alex), а не 300.
    """
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY не задан")
    payload = {
        "model": model, "temperature": 0, "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    last = None
    for attempt in range(attempts):
        try:
            r = _post(JUDGE_BASE.rstrip("/") + "/chat/completions",
                      {"Authorization": "Bearer " + key, "content-type": "application/json"}, payload)
            content = r["choices"][0]["message"]["content"]
            if content and content.strip():
                return content
            last = RuntimeError("пустой content от судьи")
        except Exception as e:  # noqa: BLE001 — повторяем, как у Alex
            last = e
        time.sleep(1 + attempt)
    raise RuntimeError(f"судья не ответила за {attempts} попыток: {last}")


_TOKEN_RE = re.compile(r"\b([AB])\b")


def parse_choice(first_line):
    """Первая строка ответа судьи -> 'A'|'B'|None.

    Разбор строго по первой строке (как у Alex). Допускается только оформление
    первой строки (markdown, кавычки, «Ответ B ...»), но НЕ содержимое: если в
    первой строке встречаются оба варианта (A или B) или ни одного —
    ответ считается неразборчивым (None), чтобы не выдумывать выбор.
    """
    line = first_line.strip().strip("*_`# \u00ab\u00bb\"'()[]")
    if line.upper() in ("A", "B"):
        return line.upper()
    found = {m.group(1) for m in _TOKEN_RE.finditer(line.upper())}
    if len(found) == 1:
        return found.pop()
    return None


def compare(question, ans_a, ans_b, judge_model):
    """Возвращает ('A'|'B'|None, сырой ответ)."""
    user = (f"## Запрос\n{question}\n\n## Ответ A\n{ans_a}\n\n"
            f"## Ответ B\n{ans_b}\n\nПервая строка ответа — A или B.")
    content = judge_chat(COMPARE_SYSTEM, user, judge_model)
    first = content.strip().split("\n", 1)[0]
    choice = parse_choice(first)
    if choice:
        return choice, content
    return None, content


def subject_answer(question, model):
    key = os.environ.get("MINIMAX_API_KEY", "")
    if not key:
        raise RuntimeError("MINIMAX_API_KEY не задан")
    payload = {"model": model, "max_tokens": 400,
               "messages": [{"role": "user", "content": question}]}
    r = _post(SUBJECT_BASE.rstrip("/") + "/v1/messages",
              {"x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}, payload)
    return "\n".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Калибровка смещений LLM-судьи")
    ap.add_argument("--cases", default=DEFAULT_CASES)
    ap.add_argument("--judge", default=JUDGE_MODEL)
    ap.add_argument("--subject", default=SUBJECT_MODEL)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip-self-preference", action="store_true")
    ap.add_argument("--skip-cross-model", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    with open(args.cases, encoding="utf-8") as f:
        data = json.load(f)
    cases = data["cases"] if isinstance(data, dict) else data
    rng = random.Random(RNG_SEED)

    rows = []
    for case in cases:
        q, good, bad = case["question"], case["good"], case["bad"]
        bad_long = case.get("bad_long") or bad
        row = {"id": case.get("id", "?")}

        # --- 1. ПОЗИЦИЯ: good=A против bad, затем good=B против bad ---
        r1, raw1 = compare(q, good, bad, args.judge)
        time.sleep(1)
        r2, raw2 = compare(q, bad, good, args.judge)
        time.sleep(1)
        row["position_r1"] = r1
        row["position_r2"] = r2
        row["raw_r1"] = raw1.strip().split("\n", 1)[0][:120]
        row["raw_r2"] = raw2.strip().split("\n", 1)[0][:120]
        row["position_consistent"] = (r1 == "A" and r2 == "B")
        row["position_flip"] = (r1 is not None and r2 is not None
                                and ((r1 == "A") != (r2 == "B")))
        row["position_unparsed"] = (r1 is None or r2 is None)

        # --- 2. ДЛИНА: good-короткий против bad-длинного; позиция good — случайна ---
        if rng.random() < 0.5:
            win, _ = compare(q, good, bad_long, args.judge)
            good_pos = "A"
        else:
            win, _ = compare(q, bad_long, good, args.judge)
            good_pos = "B"
        time.sleep(1)
        row["length_good_pos"] = good_pos
        row["length_winner"] = win
        row["length_bias"] = (win is not None and win != good_pos)

        # --- 3. САМОПРЕДПОЧТЕНИЕ: ответ самого судьи против эталона ---
        if not args.skip_self_preference:
            try:
                own = judge_chat("Ответь на вопрос кратко и по сути.", q, args.judge, 2000)
                time.sleep(1)
                if rng.random() < 0.5:
                    win, _ = compare(q, own, good, args.judge)
                    own_pos = "A"
                else:
                    win, _ = compare(q, good, own, args.judge)
                    own_pos = "B"
                time.sleep(1)
                row["self_preference_pos"] = own_pos
                row["self_preference_winner"] = win
                row["self_preference"] = (win is not None and win == own_pos)
            except Exception as e:
                row["self_preference_error"] = str(e)

        # --- 4. CROSS-MODEL (доп. мера, не в базовой линии Alex): ответ подопытной M3 ---
        if not args.skip_cross_model:
            try:
                subj = subject_answer(q, args.subject)
                cp, _ = compare(q, good, subj, args.judge)
                row["cross_model_preference"] = (cp == "B")
                row["subject_answer"] = subj[:200]
            except Exception as e:
                row["cross_model_error"] = str(e)

        rows.append(row)
        print(f"[{row['id']}] position_consistent={row['position_consistent']} "
              f"flip={row['position_flip']} length_bias={row['length_bias']} "
              f"self_pref={row.get('self_preference')} cross={row.get('cross_model_preference')}")

    n = len(rows)
    def rate(key):
        vals = [r for r in rows if key in r]
        return round(sum(1 for r in vals if r[key]) / len(vals), 3) if vals else None

    metrics = {
        "position_consistency": rate("position_consistent"),
        "position_flip": rate("position_flip"),
        "length_bias": rate("length_bias"),
        "self_preference": rate("self_preference"),
        "cross_model_preference": rate("cross_model_preference"),
    }
    thresholds = {"position_consistency": 1.0, "position_flip": 0.0,
                  "length_bias": 0.25, "self_preference": 0.5}
    measures = []
    if (metrics["position_flip"] or 0) > thresholds["position_flip"] or \
            (metrics["position_consistency"] or 0) < thresholds["position_consistency"]:
        measures.append("позиция: прогонять сравнение в обоих порядках и "
                        "усреднять (swap-вотинг) либо фиксировать seeded-порядок "
                        "и логировать его в verdict")
    if (metrics["length_bias"] or 0) > thresholds["length_bias"]:
        measures.append("длина: в рубрику судьи явное «длина не является "
                        "признаком качества» + нормировка ответов перед "
                        "сравнением (обрезка boilerplate)")
    if (metrics["self_preference"] or 0) > thresholds["self_preference"]:
        measures.append("самопредпочтение: судья ≠ генератор ответов; "
                        "маскировка источника (уже есть в гонках — сохранить)")
    if not measures:
        measures.append("смещений выше порогов не обнаружено; текущие меры "
                        "(seeded-анонимизация в гонках) достаточны")

    summary = {"judge": args.judge, "subject": args.subject, "cases": n,
               "metrics": metrics, "thresholds": thresholds, "measures": measures, "rows": rows}
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"отчёт: {args.out}")
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("ИТОГ: " + " · ".join(f"{k}={v}" for k, v in metrics.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
