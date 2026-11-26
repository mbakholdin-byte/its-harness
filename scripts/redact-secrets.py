#!/usr/bin/env python3
"""
redact-secrets.py — redact API keys, tokens, and credentials from text.

Inspired by Hermes Agent's `redact_sensitive_text()` (NousResearch/hermes-agent/run_agent.py:2162-2190).
Ported to standalone stdlib-only Python for Mavis pipeline integration.

Usage:
    # Read from stdin, write to stdout
    echo "my key is sk-abc123" | python redact-secrets.py

    # Read file, overwrite in-place (with .bak)
    python redact-secrets.py --in-place path/to/file.txt

    # Read file, write to new file
    python redact-secrets.py input.txt output.txt

    # Show what was redacted (audit mode)
    python redact-secrets.py --audit path/to/file.txt

Patterns covered (best-effort, see test_redact-secrets.py for examples):
    - OpenAI: sk-..., sk-proj-...
    - Anthropic: sk-ant-...
    - Google AI: AIza...
    - GitHub: ghp_..., gho_..., ghu_..., ghs_..., ghr_...
    - Slack: xox[bp]-..., xoxa-..., xoxr-...
    - Stripe: sk_live_..., sk_test_..., pk_live_..., pk_test_...
    - HuggingFace: hf_...
    - Tavily: tvly-dev/prod-... (2026-11-11, реальный кейс утечки у Hermes)
    - Jina: jina_... · Fireworks: fw_... · Voyage: va-...
    - AWS: AKIA[0-9A-Z]{16}
    - Bearer tokens in HTTP headers
    - JSON keys: "api_key", "token", "password", "secret"
    - PEM private keys
    - Generic high-entropy 32+ char hex/base64 strings (opt-in, --strict)
    - HEX 40+ без префикса — в режиме --audit по умолчанию (аудит допускает false-positive)

ОГРАНИЧЕНИЕ (2026-11-11, ревизия Hermes): инструмент value-based — структуру
YAML/JSON не видит; секреты в поддеревьях с произвольными именами детей
(например, keys: → tavily: <значение>) ловятся только по форме самого значения.
Для конфигов сочетать с key-name проходом (референс fail-closed генератора:
redact-config.py у Hermes, hermes:skills/.../harness-quality-gates/scripts/).

License: MIT (same as Hermes Agent).
"""
import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Tuple

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Pattern → (redaction_label, regex)
# Order matters: more specific patterns first, so sk-ant-* is matched before sk-*
PATTERNS: List[Tuple[str, str]] = [
    # Cloud provider keys (specific first, generic sk- LAST among sk- family)
    ("ANTHROPIC", r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    ("OPENAI", r"sk-(?!ant-)(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    ("GOOGLE_AI", r"AIza[A-Za-z0-9_\-]{35}"),
    ("GITHUB", r"gh[prous]_[A-Za-z0-9]{36,}"),
    ("SLACK", r"xox[abprs]-[A-Za-z0-9\-]+"),
    ("STRIPE", r"[sp]k_(?:live|test)_[A-Za-z0-9]{20,}"),
    ("HUGGINGFACE", r"hf_[A-Za-z0-9]{20,}"),
    ("TAVILY", r"tvly-(?:dev|prod)-[A-Za-z0-9\-]{20,}"),
    ("JINA", r"jina_[A-Za-z0-9]{20,}"),
    ("FIREWORKS", r"fw_[A-Za-z0-9]{20,}"),
    ("VOYAGE", r"va-[A-Za-z0-9]{20,}"),
    ("AWS", r"AKIA[0-9A-Z]{16}"),
    # Generic auth
    ("BEARER", r"Bearer\s+[A-Za-z0-9_\-\.=]{20,}"),
    ("PEM_KEY", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----[\s\S]+?-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    # JSON key=value (in case someone embeds secrets in JSON without quotes)
    ("JSON_KEY", r"\"(?:api[_-]?key|token|password|secret|access[_-]?key|auth[_-]?token)\"\s*:\s*\"[^\"]{6,}\""),
]


def redact(text: str, strict: bool = False) -> str:
    """Apply all redaction patterns to text. Returns redacted text."""
    if not text:
        return text
    redacted = text
    for label, pattern in PATTERNS:
        # Replace with [REDACTED-LABEL] preserving no other context
        redacted = re.sub(pattern, f"[REDACTED-{label}]", redacted)
    if strict:
        # Also catch generic high-entropy 32+ hex/base64 strings NOT already redacted
        # Be conservative: only flag long hex sequences that look like keys
        redacted = re.sub(
            r"(?<![A-Za-z0-9_])[A-Fa-f0-9]{40,}(?![A-Za-z0-9_])",
            "[REDACTED-HEX]",
            redacted,
        )
    return redacted


def redact_audit(text: str) -> Tuple[str, List[dict]]:
    """Like redact(), but also returns list of {label, count} for each pattern matched.
    В режиме аудита дополнительно ловит голые hex 40+ (вендорские ключи без
    префикса; false-positive допустимы — аудит, не redact, T-0169/hermes-002)."""
    findings = []
    redacted = text
    for label, pattern in PATTERNS:
        matches = re.findall(pattern, text)
        if matches:
            findings.append({"label": label, "count": len(matches)})
            redacted = re.sub(pattern, f"[REDACTED-{label}]", redacted)
    hex_matches = re.findall(r"(?<![A-Za-z0-9_])[A-Fa-f0-9]{40,}(?![A-Za-z0-9_])",
                             redacted)
    if hex_matches:
        findings.append({"label": "HEX40", "count": len(hex_matches)})
        redacted = re.sub(r"(?<![A-Za-z0-9_])[A-Fa-f0-9]{40,}(?![A-Za-z0-9_])",
                          "[REDACTED-HEX]", redacted)
    return redacted, findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Redact API keys, tokens, and credentials from text/stdin/file."
    )
    parser.add_argument("input", nargs="?", help="Input file (default: stdin)")
    parser.add_argument("output", nargs="?", help="Output file (default: stdout)")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite input file in place (creates .bak backup)",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Audit mode: print JSON of what was redacted (no text output)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Strict mode: also catch generic high-entropy 40+ hex strings",
    )
    args = parser.parse_args()

    # Read input
    if args.input:
        text = Path(args.input).read_text(encoding="utf-8", errors="replace")
    else:
        text = sys.stdin.read()

    # Process
    if args.audit:
        redacted, findings = redact_audit(text)
        audit_result = {
            "input_chars": len(text),
            "redacted_chars": len(redacted),
            "findings": findings,
            "total_findings": sum(f["count"] for f in findings),
        }
        print(json.dumps(audit_result, indent=2))
        return 0
    else:
        redacted = redact(text, strict=args.strict)

    # Write output
    if args.in_place and args.input:
        bak = Path(args.input).with_suffix(Path(args.input).suffix + ".bak")
        bak.write_text(text, encoding="utf-8")
        Path(args.input).write_text(redacted, encoding="utf-8")
        print(f"In-place redact: {args.input} (backup: {bak.name})", file=sys.stderr)
    elif args.output:
        Path(args.output).write_text(redacted, encoding="utf-8")
        print(f"Written: {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(redacted)

    return 0


if __name__ == "__main__":
    sys.exit(main())
