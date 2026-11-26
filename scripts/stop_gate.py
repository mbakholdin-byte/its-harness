"""Stop-hook пакета its-harness (MiniMax Code plugin V1).

Синхронный command-hook: пишет одно JSON-сообщение в stdout, код 0.
Назначение — мягкое напоминание о верификации перед выдачей результата.
Жёсткие гейты (блокировка через decision) появятся в v0.2 после пилота.
"""
import json
import sys

msg = "ITS Harness: перед финальной выдачей — self-check по auto-verify; артефакты docx/xlsx — через гейт."

sys.stdout.write(json.dumps({"systemMessage": msg}, ensure_ascii=True) + "\n")
sys.exit(0)
