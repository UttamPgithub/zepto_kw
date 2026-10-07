# -*- coding: utf-8 -*-
import os
import sys
import json
import shutil
import subprocess
import threading
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Ensure UTF-8 output encoding across platforms
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Optional curl_cffi support
try:
    from curl_cffi import requests as curl_requests

    _CURL_CFFI_AVAILABLE = True
except Exception as _e:
    curl_requests = None
    _CURL_CFFI_AVAILABLE = False
    print(f"[WARN] curl_cffi not available: {_e}")

app = Flask(__name__)

# Locate curl-impersonate binary if configured
CURL_IMPERSONATE_PATH = os.getenv("CURL_IMPERSONATE_PATH", "curl-impersonate-chrome.exe")
_CURL_IMPERSONATE_BIN = shutil.which(CURL_IMPERSONATE_PATH) or (
    CURL_IMPERSONATE_PATH if os.path.isfile(CURL_IMPERSONATE_PATH) else None
)


class _ImpersonatedResponse:
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.text = body

    def json(self):
        return json.loads(self.text)


def _post_via_curl_impersonate(url: str, headers: Dict[str, str], json_payload: Any,
                               proxy: Optional[Dict[str, str]] = None, timeout: int = 60) -> _ImpersonatedResponse:
    cmd = [
        _CURL_IMPERSONATE_BIN, "-s", "-o", "-", "-w", "\n__STATUS__%{http_code}",
        "-X", "POST", "--max-time", str(timeout)
    ]

    # Filter out problematic auto-generated headers
    filtered_headers = {k: v for k, v in headers.items() if k.lower() not in ["content-type", "content-length"]}
    for k, v in filtered_headers.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd += ["-H", "Content-Type: application/json"]

    if proxy:
        proxy_url = proxy.get("https") or proxy.get("http")
        if proxy_url:
            cmd += ["-x", proxy_url]

    cmd += ["-d", json.dumps(json_payload), url]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    out = result.stdout
    marker = "\n__STATUS__"
    idx = out.rfind(marker)

    if idx == -1:
        error_msg = result.stderr.strip() or "Empty response or execution failed"
        raise RuntimeError(f"curl-impersonate execution failed: {error_msg}")

    body = out[:idx]
    status_str = out[idx + len(marker):].strip()
    status_code = int(status_str) if status_str.isdigit() else 500

    return _ImpersonatedResponse(status_code, body)


_thread_local = threading.local()


def get_thread_session(proxy: Optional[Dict[str, str]] = None):
    proxy_url = (proxy.get("https") or proxy.get("http")) if proxy else None
    if not hasattr(_thread_local, "sessions"):
        _thread_local.sessions = {}

    cache_key = proxy_url or "direct"
    if cache_key in _thread_local.sessions:
        return _thread_local.sessions[cache_key]

    if _CURL_CFFI_AVAILABLE:
        session = curl_requests.Session(impersonate="chrome120")
        if proxy_url:
            session.proxies = {"http": proxy_url, "https": proxy_url}
        _thread_local.sessions[cache_key] = session
        return session
    else:
        session = requests.Session()
        retry_strategy = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=20, pool_maxsize=20)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        if proxy:
            session.proxies = proxy
        _thread_local.sessions[cache_key] = session
        return session


def impersonated_post(url: str, headers: Dict[str, str], json_payload: Any, proxy: Optional[Dict[str, str]] = None,
                      timeout: int = 60):
    if _CURL_IMPERSONATE_BIN:
        return _post_via_curl_impersonate(url, headers, json_payload, proxy=proxy, timeout=timeout)
    session = get_thread_session(proxy)
    return session.post(url, headers=headers, json=json_payload, timeout=timeout)


@app.route("/api/crawl", methods=["POST"])
def crawl_endpoint():
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"status": "error", "message": "Invalid or missing JSON payload"}), 400

    target_url = data.get("url")
    headers = data.get("headers", {})
    payload = data.get("payload", {})
    proxy = data.get("proxy", None)
    timeout = int(data.get("timeout", 60))

    if not target_url:
        return jsonify({"status": "error", "message": "'url' parameter is required"}), 400

    try:
        resp = impersonated_post(
            url=target_url,
            headers=headers,
            json_payload=payload,
            proxy=proxy,
            timeout=timeout
        )

        # ----------------------------------------------------
        # SAFE RESPONSE EXTRACTION (Fixes "not JSON serializable")
        # ----------------------------------------------------
        status_code = getattr(resp, "status_code", 200)

        # Try to parse the upstream body as JSON; fall back to text string
        response_body = None
        if hasattr(resp, "json") and callable(resp.json):
            try:
                response_body = resp.json()
            except Exception:
                response_body = resp.text
        elif hasattr(resp, "text"):
            response_body = resp.text
        else:
            response_body = str(resp)

        return jsonify({
            "status": "success",
            "status_code": status_code,
            "data": response_body
        }), 200

    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": f"Timed out after {timeout}s"}), 504
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)