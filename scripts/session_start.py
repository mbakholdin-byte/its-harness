"""SessionStart-hook пакета its-harness (MiniMax Code plugin V1).

Печатает одно JSON-сообщение systemMessage в stdout (уведомление в TUI,
в контекст модели не попадает) и завершается кодом 0.
Напоминание о протоколе памяти: бутлодер -> INDEX -> topics/journal.
"""
import json
import os
import sys

plugin_root = os.environ.get("PLUGIN_ROOT", "")
memory_index = os.path.join(plugin_root, "memory", "INDEX.md")

if os.path.exists(memory_index):
    msg = "ITS Harness: память найдена (memory/INDEX.md). Протокол: agent.md -> INDEX -> topics -> journal."
else:
    msg = "ITS Harness: память не инициализирована. Запустите bootstrap-опросник для настройки memory/."

sys.stdout.write(json.dumps({"systemMessage": msg}, ensure_ascii=True) + "\n")
sys.exit(0)
