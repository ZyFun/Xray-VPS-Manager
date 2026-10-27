"""Единый urllib JSON-клиент; политика ошибок остаётся у вызывающего сценария."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import urllib.request

_opener = ContextVar('http_opener', default=None)


@contextmanager
def fake(opener):
    """Обратимая подмена транспорта, включая вложенные Sandbox."""
    token = _opener.set(opener)
    try:
        yield
    finally:
        _opener.reset(token)


def http_json(url: str, timeout: float = 20, *, user_agent: str = 'xray-vps-manager'):
    request = urllib.request.Request(url, headers={'User-Agent': user_agent})
    opener = _opener.get() or urllib.request.urlopen
    with opener(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))
