"""Demo object upload service; all storage writes are confined to MinIO."""
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, unquote

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CollectorRegistry, Counter, Histogram, CONTENT_TYPE_LATEST, generate_latest

MAX_FILE = 5 * 1024 * 1024
# Includes multipart framing; file data itself is limited separately.
MAX_BODY = MAX_FILE + 64 * 1024
# S3 stores user metadata in HTTP headers with a limited total size. Leave
# ample room for the key and other headers, even for multibyte filenames.
MAX_ENCODED_FILENAME = 768
logger = logging.getLogger("uploader")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
registry = CollectorRegistry()
requests_total = Counter("app_http_requests_total", "HTTP requests", ["service", "method", "path", "status"], registry=registry)
latency = Histogram("app_http_request_duration_seconds", "HTTP request duration", ["service", "method", "path"], registry=registry)
uploads_total = Counter("app_uploads_total", "Successfully stored files", registry=registry)
app = FastAPI(title="Demo uploader")


class LimitBody:
    """Bound streamed request data before the multipart parser can spool it."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/api/uploader/files":
            return await self.app(scope, receive, send)
        data = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            data.extend(message.get("body", b""))
            if len(data) > MAX_BODY:
                return await JSONResponse({"detail": "Request too large"}, status_code=413)(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(data), "more_body": False}
            return await receive()
        await self.app(scope, replay, send)


app.add_middleware(LimitBody)


@app.middleware("http")
async def observe(request: Request, call_next):
    start = time.monotonic()
    try:
        request_id = str(uuid.UUID(request.headers.get("x-request-id", "")))
    except ValueError:
        request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        path = request.scope.get("route").path if request.scope.get("route") else "unmatched"
        if path not in ("/health/live", "/health/ready", "/metrics"):
            requests_total.labels("uploader", request.method, path, str(status)).inc()
            latency.labels("uploader", request.method, path).observe(time.monotonic() - start)
            logger.info(json.dumps({"service": "uploader", "level": "ERROR" if status >= 500 else "INFO", "request_id": request_id, "method": request.method, "path": path, "status": status, "timestamp": datetime.now(timezone.utc).isoformat()}))


async def authorize(request: Request, authorization: str = Header(default="")):
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Invalid token")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            result = await client.post(os.getenv("SECURITY_URL", "http://security:8000").rstrip("/") + "/api/security/verify", headers={"Authorization": authorization, "X-Request-ID": request.state.request_id})
        if result.status_code == 401:
            raise HTTPException(401, "Invalid token")
        if result.status_code != 200 or not result.json().get("valid"):
            raise HTTPException(503, "Authorization unavailable")
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Authorization unavailable")


def store():
    access = os.getenv("MINIO_ROOT_USER", "")
    secret = os.getenv("MINIO_ROOT_PASSWORD", "")
    if not access or not secret:
        raise HTTPException(503, "Object store is not configured")
    endpoint = os.getenv("MINIO_ENDPOINT", "minio:9000")
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access, aws_secret_access_key=secret, region_name="us-east-1", config=Config(connect_timeout=1, read_timeout=1, retries={"total_max_attempts": 1}))


def bucket():
    return os.getenv("MINIO_BUCKET", "uploads")


def ensure_bucket(client):
    try:
        client.head_bucket(Bucket=bucket())
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket", "NotFound"):
            raise
        try:
            client.create_bucket(Bucket=bucket())
        except ClientError as create_exc:
            if create_exc.response.get("Error", {}).get("Code") not in ("BucketAlreadyOwnedByYou",):
                raise


def object_id(value):
    try:
        parsed = uuid.UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError
        return value
    except ValueError:
        raise HTTPException(404, "File not found")


def put_file(key, data, name):
    client = store()
    ensure_bucket(client)
    client.put_object(Bucket=bucket(), Key=key, Body=data, ContentType="application/octet-stream", Metadata={"filename": quote(name, safe="")})


def safe_filename(filename):
    name = (filename or "file").replace("\\", "/").split("/")[-1]
    clean = []
    encoded_length = 0
    for character in name:
        if not character.isprintable():
            continue
        length = len(quote(character, safe=""))
        if encoded_length + length > MAX_ENCODED_FILENAME:
            break
        clean.append(character)
        encoded_length += length
    return "".join(clean) or "file"


def stream_body(body):
    try:
        while True:
            chunk = body.read(64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        body.close()


@app.post("/api/uploader/files", dependencies=[Depends(authorize)], status_code=201)
async def upload(file: UploadFile = File(...)):
    data = await file.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise HTTPException(413, "File exceeds 5 MiB")
    # Strip client-supplied paths and control characters; the generated key is the only storage path.
    name = safe_filename(file.filename)
    key = str(uuid.uuid4())
    try:
        await run_in_threadpool(put_file, key, data, name)
    except (ClientError, BotoCoreError):
        raise HTTPException(503, "Object store unavailable")
    uploads_total.inc()
    return {"id": key, "filename": name, "size": len(data)}


@app.get("/api/uploader/files", dependencies=[Depends(authorize)])
def list_files():
    try:
        client = store()
        ensure_bucket(client)
        files = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket()):
            for obj in page.get("Contents", []):
                metadata = client.head_object(Bucket=bucket(), Key=obj["Key"]).get("Metadata", {})
                files.append({"id": obj["Key"], "filename": unquote(metadata.get("filename", "file")), "size": obj["Size"], "last_modified": obj["LastModified"].isoformat()})
        return {"files": files}
    except (ClientError, BotoCoreError):
        raise HTTPException(503, "Object store unavailable")


@app.get("/api/uploader/files/{file_id}", dependencies=[Depends(authorize)])
def download(file_id: str):
    key = object_id(file_id)
    try:
        result = store().get_object(Bucket=bucket(), Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
            raise HTTPException(404, "File not found")
        raise HTTPException(503, "Object store unavailable")
    except BotoCoreError:
        raise HTTPException(503, "Object store unavailable")
    filename = unquote(result.get("Metadata", {}).get("filename", "file"))
    return StreamingResponse(stream_body(result["Body"]), media_type="application/octet-stream", headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe=""), "X-Content-Type-Options": "nosniff"})


@app.get("/health/live")
async def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    try:
        store().list_buckets()
    except (ClientError, BotoCoreError):
        raise HTTPException(503, "Object store unavailable")
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
