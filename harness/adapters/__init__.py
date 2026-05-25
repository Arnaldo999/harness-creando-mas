"""Adapters de canales externos hacia el harness.

Cada adapter convierte el formato wire de un canal (Telegram, Slack,
WhatsApp, etc.) a una invocación canónica del `Agent` y devuelve la
respuesta por el mismo canal.

Fase 3:
- `telegram` — webhook de @arnaldo_agente_bot.
"""
