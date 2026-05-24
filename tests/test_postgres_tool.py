"""Tests del validador SQL y la inyección de tenant_slug.

No tocamos asyncpg real — testeamos puro validador + transformaciones
de string. La ejecución E2E contra Postgres queda para validación manual
post-deploy.
"""

from __future__ import annotations

import pytest

from harness.tool.ecosystem.postgres import (
    SQLValidationError,
    ensure_limit,
    inject_tenant_filter,
    validate_select_sql,
)


# -------------------------------------------------------------------------
# Validador SQL
# -------------------------------------------------------------------------


class TestValidateSelectSQL:
    def test_select_simple_pasa(self) -> None:
        sql = "SELECT id, nombre FROM leads"
        assert validate_select_sql(sql) == sql

    def test_with_cte_pasa(self) -> None:
        sql = "WITH x AS (SELECT 1) SELECT * FROM x"
        assert validate_select_sql(sql) == sql

    def test_select_con_trailing_semicolon_se_remueve(self) -> None:
        sql = "SELECT 1;"
        assert validate_select_sql(sql) == "SELECT 1"

    def test_multiples_statements_rechazado(self) -> None:
        sql = "SELECT 1; SELECT 2;"
        with pytest.raises(SQLValidationError, match="múltiples statements"):
            validate_select_sql(sql)

    def test_drop_table_rechazado(self) -> None:
        with pytest.raises(SQLValidationError, match="DROP"):
            validate_select_sql("DROP TABLE leads")

    def test_drop_dentro_de_select_rechazado(self) -> None:
        # Un statement stacking con DROP debe ser rechazado — la primera
        # defensa que lo pesca es el guard de ';' interno, no la regex
        # destructiva. Ambas son correctas; lo importante es que NO pase.
        with pytest.raises(SQLValidationError):
            validate_select_sql("SELECT * FROM leads; DROP TABLE leads")

    def test_delete_rechazado(self) -> None:
        with pytest.raises(SQLValidationError, match="DELETE"):
            validate_select_sql("DELETE FROM leads")

    def test_update_rechazado(self) -> None:
        with pytest.raises(SQLValidationError, match="UPDATE"):
            validate_select_sql("UPDATE leads SET estado='contactado' WHERE id=1")

    def test_insert_rechazado(self) -> None:
        with pytest.raises(SQLValidationError, match="INSERT"):
            validate_select_sql("INSERT INTO leads (id) VALUES (1)")

    def test_truncate_rechazado(self) -> None:
        with pytest.raises(SQLValidationError, match="TRUNCATE"):
            validate_select_sql("TRUNCATE leads")

    def test_create_rechazado(self) -> None:
        with pytest.raises(SQLValidationError, match="CREATE"):
            validate_select_sql("CREATE TABLE x (id INT)")

    def test_alter_rechazado(self) -> None:
        with pytest.raises(SQLValidationError, match="ALTER"):
            validate_select_sql("ALTER TABLE leads ADD COLUMN foo INT")

    def test_created_at_column_no_matchea_destructivo(self) -> None:
        # Regresión del bug del n8n original: regex sin word boundaries
        # matcheaba "CREATE" contra "created_at". Con \b no debería.
        sql = "SELECT id, created_at FROM leads WHERE created_at > NOW() - INTERVAL '7 days'"
        assert validate_select_sql(sql) == sql

    def test_creator_column_no_matchea_create(self) -> None:
        sql = "SELECT creator_id FROM contratos"
        assert validate_select_sql(sql) == sql

    def test_deleted_at_no_matchea_delete(self) -> None:
        sql = "SELECT deleted_at FROM leads WHERE deleted_at IS NULL"
        assert validate_select_sql(sql) == sql

    def test_updates_table_no_matchea_update(self) -> None:
        # Hipotético: si existiera una tabla `updates`, no debe rechazarse.
        sql = "SELECT * FROM updates_log"
        assert validate_select_sql(sql) == sql

    def test_query_vacia_rechazada(self) -> None:
        with pytest.raises(SQLValidationError, match="vacía"):
            validate_select_sql("")

    def test_no_select_ni_with_rechazado(self) -> None:
        with pytest.raises(SQLValidationError, match="solo se permite SELECT"):
            validate_select_sql("EXPLAIN ANALYZE SELECT 1")

    def test_exec_rechazado(self) -> None:
        with pytest.raises(SQLValidationError, match="EXEC"):
            validate_select_sql("EXEC sp_foo")

    def test_replace_rechazado(self) -> None:
        with pytest.raises(SQLValidationError, match="REPLACE"):
            validate_select_sql("REPLACE INTO leads (id) VALUES (1)")


# -------------------------------------------------------------------------
# ensure_limit
# -------------------------------------------------------------------------


class TestEnsureLimit:
    def test_agrega_limit_si_no_hay(self) -> None:
        sql = "SELECT * FROM leads"
        assert ensure_limit(sql) == "SELECT * FROM leads LIMIT 100"

    def test_respeta_limit_existente(self) -> None:
        sql = "SELECT * FROM leads LIMIT 5"
        assert ensure_limit(sql) == sql

    def test_respeta_limit_uppercase(self) -> None:
        sql = "select * from leads LIMIT 7"
        assert ensure_limit(sql) == sql


# -------------------------------------------------------------------------
# inject_tenant_filter
# -------------------------------------------------------------------------


class TestInjectTenantFilter:
    def test_inyecta_en_tabla_con_tenant(self) -> None:
        sql = "SELECT * FROM leads"
        out = inject_tenant_filter(sql, "demo", {"leads"})
        assert "tenant_slug = 'demo'" in out
        assert "WHERE" in out

    def test_no_inyecta_si_tabla_no_tiene_tenant(self) -> None:
        sql = "SELECT * FROM otra_tabla"
        out = inject_tenant_filter(sql, "demo", {"leads", "propiedades"})
        assert "tenant_slug" not in out
        assert out == sql

    def test_no_duplica_si_ya_filtra(self) -> None:
        sql = "SELECT * FROM leads WHERE tenant_slug = 'demo' AND score = 'caliente'"
        out = inject_tenant_filter(sql, "demo", {"leads"})
        assert out.count("tenant_slug") == 1

    def test_inyecta_con_where_existente(self) -> None:
        sql = "SELECT * FROM leads WHERE score = 'caliente'"
        out = inject_tenant_filter(sql, "demo", {"leads"})
        assert "tenant_slug = 'demo'" in out
        assert "score = 'caliente'" in out

    def test_inyecta_respetando_order_by(self) -> None:
        sql = "SELECT * FROM leads ORDER BY created_at DESC"
        out = inject_tenant_filter(sql, "demo", {"leads"})
        assert "tenant_slug = 'demo'" in out
        # ORDER BY debe seguir presente y DESPUÉS del WHERE inyectado.
        assert out.index("ORDER BY") > out.index("tenant_slug")

    def test_inyecta_respetando_limit(self) -> None:
        sql = "SELECT * FROM leads LIMIT 10"
        out = inject_tenant_filter(sql, "demo", {"leads"})
        assert "tenant_slug = 'demo'" in out
        assert out.index("LIMIT") > out.index("tenant_slug")

    def test_slug_invalido_rechazado(self) -> None:
        with pytest.raises(SQLValidationError, match="tenant_slug inválido"):
            inject_tenant_filter("SELECT * FROM leads", "demo'; DROP--", {"leads"})

    def test_join_inyecta_si_alguna_tabla_es_tenant(self) -> None:
        sql = "SELECT l.id FROM leads l JOIN propiedades p ON p.id = l.propiedad_id"
        out = inject_tenant_filter(sql, "demo", {"leads", "propiedades"})
        assert "tenant_slug = 'demo'" in out

    def test_tenant_filter_en_in_clausula_se_detecta(self) -> None:
        sql = "SELECT * FROM leads WHERE tenant_slug IN ('demo','otro')"
        out = inject_tenant_filter(sql, "demo", {"leads"})
        # No re-inyecta.
        assert out.count("tenant_slug") == 1
