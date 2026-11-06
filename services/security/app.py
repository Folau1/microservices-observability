"""Demo identity service. Credentials and signing key are supplied externally."""
import hmac
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from prometheus_client import CollectorRegistry, Counter, Histogram, CONTENT_TYPE_LATEST, generate_latest

logger = logging.getLogger("security")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
registry = CollectorRegistry()
requests_total = Counter("app_http_requests_total", "HTTP requests", ["service", "method", "path", "status"], registry=registry)
latency = Histogram("app_http_request_duration_seconds", "HTTP request duration", ["service", "method", "path"], registry=registry)
app = FastAPI(title="Demo security")


@app.middleware("http")
async def observe(request: Request, call_next):
    start = time.monotonic()
    try:
        request_id = str(uuid.UUID(request.headers.get("x-request-id", "")))
    except ValueError:
        request_id = str(uuid.uuid4())
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        path = request.scope.get("route").path if request.scope.get("route") else "unmatched"
        if path not in ("/health", "/metrics"):
            requests_total.labels("security", request.method, path, str(status)).inc()
            latency.labels("security", request.method, path).observe(time.monotonic() - start)
            logger.info(json.dumps({"service": "security", "level": "ERROR" if status >= 500 else "INFO", "request_id": request_id, "method": request.method, "path": path, "status": status, "timestamp": datetime.now(timezone.utc).isoformat()}))


def settings():
    username, password, secret = (os.getenv(k, "") for k in ("DEMO_USERNAME", "DEMO_PASSWORD", "TOKEN_SECRET"))
    if not username or not password or len(secret) < 32:
        raise HTTPException(503, "Security service is not configured")
    return username, password, secret


class Credentials(BaseModel):
    username: str
    password: str


@app.post("/api/security/login")
def login(credentials: Credentials):
    username, password, secret = settings()
    # Compare both fields and never emit credentials in logs.
    user_ok = hmac.compare_digest(credentials.username.encode(), username.encode())
    pass_ok = hmac.compare_digest(credentials.password.encode(), password.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(401, "Invalid credentials")
    now = datetime.now(timezone.utc)
    token = jwt.encode({"sub": username, "iat": now, "exp": now + timedelta(minutes=30), "iss": "demo-security"}, secret, algorithm="HS256")
    return {"access_token": token, "token_type": "bearer"}


@app.post("/api/security/verify")
def verify(authorization: str = Header(default="")):
    _, _, secret = settings()
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Invalid token")
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], issuer="demo-security", options={"require": ["exp", "iat", "sub", "iss"]})
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    return {"valid": True, "subject": claims["sub"]}


@app.get("/health")
def health():
    settings()
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
