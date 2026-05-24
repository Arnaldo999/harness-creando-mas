"""Paquete `tool` — interfaz Tool + Registry.

En modo HTTP NO auto-registramos tools como hace `byo-harness-python`
(porque la lista de tools enabled depende del tenant). El bootstrap del
tenant decide cuáles instanciar y registrar — ver `harness.tenant.loader`.

Las tools del ecosistema (Postgres, lead helpers, Tavily) viven en
`harness.tool.ecosystem`.
"""

from harness.tool.registry import Registry, Tool

__all__ = ["Registry", "Tool"]
