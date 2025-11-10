from fastapi import HTTPException, Request, status
from .config import API_KEY, METRICS_USER, METRICS_PASS
import base64


def require_api_key(request: Request):
    if not API_KEY:
        return
    key = request.headers.get("x-api-key") or request.query_params.get("api_key")
    if key != API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "invalid api key"})


def require_metrics_basic_auth(request: Request):
    if not (METRICS_USER and METRICS_PASS):
        return
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        raise HTTPException(status_code=401, detail={"error": "basic auth required"}, headers={"WWW-Authenticate": "Basic"})
    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
        user, pwd = decoded.split(":", 1)
        if not (user == METRICS_USER and pwd == METRICS_PASS):
            raise ValueError("bad creds")
    except Exception:
        raise HTTPException(status_code=401, detail={"error": "invalid credentials"}, headers={"WWW-Authenticate": "Basic"})
