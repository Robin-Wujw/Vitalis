import asyncio
import importlib

import pytest

from vitalis.adapters.persistence.database import SchemaMismatch


api_module = importlib.import_module("vitalis.entrypoints.api.app")


def test_health_probes_do_not_claim_source_data_is_fresh(client, monkeypatch):
    assert client.get("/live").json() == {"status": "live"}
    assert client.get("/ready").json() == {"status": "ready"}
    assert client.get("/healthz").status_code == 404
    def refuse():
        raise SchemaMismatch("old schema")

    monkeypatch.setattr(api_module, "check_schema", refuse)
    assert client.get("/live").status_code == 200
    ready = client.get("/ready")
    assert ready.status_code == 503
    assert "old schema" not in ready.text


def test_api_lifespan_refuses_unknown_schema_before_serving(monkeypatch):
    def refuse():
        raise SchemaMismatch("old schema")

    monkeypatch.setattr(api_module, "check_schema", refuse)

    async def start():
        async with api_module.lifespan(api_module.app):
            raise AssertionError("startup should reject an unknown schema")

    with pytest.raises(SchemaMismatch, match="old schema"):
        asyncio.run(start())
