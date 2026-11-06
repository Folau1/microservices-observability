"""Contract tests: no external services or actual secrets required."""
import importlib.util
import io
import asyncio
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import jwt
import pytest
import httpx
from botocore.exceptions import EndpointConnectionError
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "services" / name / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def security(monkeypatch):
    monkeypatch.setenv("DEMO_USERNAME", "test-user")
    monkeypatch.setenv("DEMO_PASSWORD", "test-password")
    monkeypatch.setenv("TOKEN_SECRET", "test-secret-that-is-at-least-32-bytes")
    return load("security")


def test_security_login_and_verify(security):
    client = TestClient(security.app)
    assert client.post("/api/security/login", json={"username": "test-user", "password": "wrong"}).status_code == 401
    response = client.post("/api/security/login", json={"username": "test-user", "password": "test-password"})
    assert response.status_code == 200
    token = response.json()["access_token"]
    assert client.post("/api/security/verify", headers={"Authorization": f"Bearer {token}"}).json()["valid"]
    assert client.post("/api/security/verify", headers={"Authorization": "Bearer invalid"}).status_code == 401
    expired = jwt.encode({"sub": "test-user", "iat": 1, "exp": 2, "iss": "demo-security"}, "test-secret-that-is-at-least-32-bytes", algorithm="HS256")
    assert client.post("/api/security/verify", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    assert "app_http_requests_total" in client.get("/metrics").text


def test_security_fails_closed_without_key(security, monkeypatch):
    monkeypatch.delenv("TOKEN_SECRET")
    assert TestClient(security.app).get("/health").status_code == 503


class MemoryStore:
    def __init__(self):
        self.objects = {}

    def head_bucket(self, **kwargs):
        pass

    def list_buckets(self):
        return {}

    def put_object(self, Key, Body, Metadata, **kwargs):
        self.objects[Key] = (Body, Metadata)

    def get_object(self, Key, **kwargs):
        body, metadata = self.objects[Key]
        return {"Body": io.BytesIO(body), "Metadata": metadata}

    def get_paginator(self, name):
        return self

    def paginate(self, **kwargs):
        return [{"Contents": [{"Key": key, "Size": len(value[0]), "LastModified": datetime.now(timezone.utc)} for key, value in self.objects.items()]}]

    def head_object(self, Key, **kwargs):
        return {"Metadata": self.objects[Key][1]}


@pytest.fixture
def uploader(monkeypatch):
    module = load("uploader")
    memory = MemoryStore()
    monkeypatch.setattr(module, "store", lambda: memory)
    async def allow():
        return None
    module.app.dependency_overrides[module.authorize] = allow
    return module


def test_upload_list_download_and_size_limit(uploader):
    client = TestClient(uploader.app)
    response = client.post("/api/uploader/files", files={"file": ("пример.txt", b"hello", "text/plain")})
    assert response.status_code == 201
    file_id = response.json()["id"]
    assert client.get("/api/uploader/files").json()["files"][0]["filename"] == "пример.txt"
    downloaded = client.get(f"/api/uploader/files/{file_id}")
    assert downloaded.content == b"hello"
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert client.get("/api/uploader/files/..%2Fsecret").status_code in (404, 405)
    too_large = client.post("/api/uploader/files", files={"file": ("big", b"x" * (uploader.MAX_FILE + 1))})
    assert too_large.status_code == 413
    assert "app_uploads_total 1.0" in client.get("/metrics").text


def test_long_unicode_filename_fits_metadata_and_roundtrips(uploader):
    client = TestClient(uploader.app)
    original = "🗂" * 200 + ".txt"
    response = client.post("/api/uploader/files", files={"file": (original, b"content")})
    assert response.status_code == 201
    result = response.json()
    assert result["filename"]
    assert original.startswith(result["filename"])
    assert len(quote(result["filename"], safe="")) <= uploader.MAX_ENCODED_FILENAME
    metadata = uploader.store().objects[result["id"]][1]
    assert metadata["filename"] == quote(result["filename"], safe="")
    assert client.get("/api/uploader/files").json()["files"][0]["filename"] == result["filename"]
    assert uploader.safe_filename("/\n\t") == "file"


def test_download_stream_closes_body_on_completion_and_early_stop(uploader, monkeypatch):
    body = io.BytesIO(b"example")
    class StoreWithBody:
        def get_object(self, **kwargs):
            return {"Body": body, "Metadata": {"filename": "example.txt"}}

    monkeypatch.setattr(uploader, "store", StoreWithBody)
    response = TestClient(uploader.app).get("/api/uploader/files/00000000-0000-4000-8000-000000000001")
    assert response.status_code == 200
    assert response.content == b"example"
    assert body.closed

    partially_read = io.BytesIO(b"x" * (64 * 1024 + 1))
    stream = uploader.stream_body(partially_read)
    assert len(next(stream)) == 64 * 1024
    stream.close()
    assert partially_read.closed


def test_uploader_rejects_missing_token(uploader):
    uploader.app.dependency_overrides.clear()
    client = TestClient(uploader.app)
    assert client.get("/api/uploader/files").status_code == 401
    assert client.post("/api/uploader/files", files={"file": ("x", b"x")}).status_code == 401


def test_health_separates_process_from_store(uploader, monkeypatch):
    client = TestClient(uploader.app)
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200

    def unavailable():
        raise EndpointConnectionError(endpoint_url="http://minio:9000")

    monkeypatch.setattr(uploader, "store", unavailable)
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 503


def test_slow_upload_does_not_block_liveness(uploader, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_put(*args):
        started.set()
        release.wait(timeout=3)

    monkeypatch.setattr(uploader, "put_file", slow_put)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=uploader.app), base_url="http://test") as client:
            upload = asyncio.create_task(client.post("/api/uploader/files", files={"file": ("a.txt", b"a")}))
            start = time.monotonic()
            try:
                assert await asyncio.to_thread(started.wait, 1)
                response = await client.get("/health/live")
                assert response.status_code == 200
                assert time.monotonic() - start < 2
            finally:
                release.set()
                await upload

    asyncio.run(exercise())


def test_uploader_rejects_invalid_token_and_auth_outage(uploader, monkeypatch):
    uploader.app.dependency_overrides.clear()

    class FakeAuthClient:
        status = 401

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            class FakeResponse:
                status_code = FakeAuthClient.status
                def json(self):
                    return {"valid": False}
            return FakeResponse()

    monkeypatch.setattr(uploader.httpx, "AsyncClient", FakeAuthClient)
    client = TestClient(uploader.app)
    assert client.get("/api/uploader/files", headers={"Authorization": "Bearer bad"}).status_code == 401
    FakeAuthClient.status = 503
    assert client.get("/api/uploader/files", headers={"Authorization": "Bearer bad"}).status_code == 503
