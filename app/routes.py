import os
import time
from typing import Dict, Any, List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from .auth import require_api_key, require_metrics_basic_auth
from .metrics import registry, METRICS_PREFIX
from .config import config, APP_VERSION, GIT_SHA, BUILD_DATE
from .logging_setup import logger
from .config import reload_config_if_changed

router = APIRouter()


@router.get("/health", response_class=PlainTextResponse)
async def health(_=Depends(require_api_key)):
    return "ok"


@router.get("/info", response_class=JSONResponse)
async def info(_=Depends(require_api_key)):
    try:
        st = os.stat(os.environ.get("CONFIG_PATH", "/app/config.yaml"))
        mtime_ns = st.st_mtime_ns
        size = st.st_size
    except FileNotFoundError:
        mtime_ns = None
        size = None
    return {
        "project": "mail-e2e-exporter",
        "version": {
            "app": APP_VERSION,
            "revision": GIT_SHA,
            "build_date": BUILD_DATE,
        },
        "config": {
            "has_config": os.path.exists(os.environ.get("CONFIG_PATH", "/app/config.yaml")),
            "mtime_ns": mtime_ns,
            "size": size,
            "tests": [t.get("name") for t in config.tests()],
        },
    }


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(_=Depends(require_metrics_basic_auth)):
    output = generate_latest(registry)
    return PlainTextResponse(content=output, media_type=CONTENT_TYPE_LATEST)


@router.get("/version", response_class=PlainTextResponse)
async def version_endpoint(_=Depends(require_api_key)):
    return APP_VERSION


# Helpers to collect samples
from prometheus_client.samples import Sample

def _collect_metric_samples(name: str) -> List[Dict[str, Any]]:
    m = registry._names_to_collectors.get(name)  # type: ignore[attr-defined]
    if not m:
        return []
    res: List[Dict[str, Any]] = []
    for metric in m.collect():
        for s in metric.samples:
            if isinstance(s, Sample):
                labels = dict(s.labels)
                res.append({"name": s.name, "labels": labels, "value": s.value})
    return res


@router.get("/errors", response_class=JSONResponse)
async def errors_endpoint(_=Depends(require_api_key)):
    errs = _collect_metric_samples(f"{METRICS_PREFIX}test_errors_total")
    last = _collect_metric_samples(f"{METRICS_PREFIX}last_error_info")

    def key_of(lbls: Dict[str, str]):
        return f"{lbls.get('route')}|{lbls.get('from')}|{lbls.get('to')}"

    def index_by_key(samples: List[Dict[str, Any]]):
        d = {}
        for s in samples:
            d[key_of(s["labels"])] = s
        return d

    idx_errs = index_by_key(errs)
    idx_last = index_by_key(last)

    out = []
    for k, v in idx_errs.items():
        e = v.copy()
        e["last_hash"] = idx_last.get(k, {}).get("value")
        out.append(e)
    return out


@router.post("/reload", response_class=JSONResponse)
async def reload_config(_=Depends(require_api_key)):
    changed = reload_config_if_changed(logger, force=True)
    return {"reloaded": changed}
