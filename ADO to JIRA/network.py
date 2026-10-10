import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def create_http_session():
    retry = Retry(
        total=4,
        connect=4,
        read=2,
        status=4,
        backoff_factor=0.5,
        backoff_max=30,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def call_with_retry(operation, *args, **kwargs):
    for attempt in range(4):
        try:
            return operation(*args, **kwargs)
        except Exception as error:
            response = getattr(error, "response", None)
            status_code = getattr(error, "status_code", None)
            if status_code is None and response is not None:
                status_code = getattr(response, "status_code", None)
            retryable = status_code in {429, 500, 502, 503, 504} or (
                status_code is None and isinstance(error, requests.RequestException)
            )
            if not retryable or attempt == 3:
                raise

            headers = getattr(response, "headers", {}) or getattr(error, "headers", {})
            retry_after = headers.get("Retry-After") if headers else None
            try:
                delay = min(max(float(retry_after), 0), 30) if retry_after else 0.5 * (2**attempt)
            except (TypeError, ValueError):
                delay = 0.5 * (2**attempt)
            time.sleep(delay)