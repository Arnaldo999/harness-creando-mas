"""Tools del ecosistema Creando Más — Postgres, leads, conversación, Tavily.

Cada módulo expone una clase Tool (subclase de `harness.tool.Tool`) que
toma su configuración en el constructor. El loader del tenant
(`harness.tenant.loader`) las instancia con las credenciales correctas
y las registra en el Registry del tenant.

Las tools genéricas del byo-harness (bash, read_file, write_file, etc.)
NO se exponen en modo HTTP — esto es por seguridad y para mantener la
superficie del LLM acotada al dominio del CRM.
"""

from harness.tool.ecosystem.conversation import GenerarResumenConversacionTool
from harness.tool.ecosystem.lead import LookupLeadTool, UpdateLeadEstadoTool
from harness.tool.ecosystem.postgres import (
    QueryPostgresTool,
    SQLValidationError,
    inject_tenant_filter,
    validate_select_sql,
)
from harness.tool.ecosystem.tavily import TavilySearchTool

__all__ = [
    "QueryPostgresTool",
    "LookupLeadTool",
    "UpdateLeadEstadoTool",
    "GenerarResumenConversacionTool",
    "TavilySearchTool",
    "SQLValidationError",
    "validate_select_sql",
    "inject_tenant_filter",
]
