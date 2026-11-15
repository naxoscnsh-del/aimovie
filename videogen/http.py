"""HTTP 封装：统一鉴权、超时、重试（429/5xx 指数退避）。"""
from __future__ import annotations

import time
from typing import Any

import requests


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} {url}: {body[:500]}")
        self.status = status
        self.body = body


RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 300.0,
                 retries: int = 3, session: requests.Session | None = None,
                 sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries
        self.session = session or requests.Session()
        self._sleep = sleep

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # 避免部分网关 gzip 截断问题
            "Accept-Encoding": "identity",
        }

    def request(self, method: str, path: str, json: Any = None) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.request(method, url, json=json,
                                            headers=self._headers(), timeout=self.timeout)
            except requests.ReadTimeout as e:
                # POST 可能已被服务端受理并计费（图片/视频生成），重试会重复扣费 → 不重试，交给上层提示
                if method.upper() == "POST":
                    raise ApiError(0, f"读取超时（请求可能已被受理，请到方舟控制台确认后再重试）：{e}", url) from e
                last = e
            except requests.RequestException as e:  # 连接失败等网络错误：请求没送达，可以重试
                last = e
            else:
                if resp.status_code < 400:
                    return resp.json()
                last = ApiError(resp.status_code, resp.text, url)
                if resp.status_code not in RETRY_STATUS:
                    raise last
            if attempt < self.retries:
                self._sleep(min(2 ** attempt * 2, 30))
        assert last is not None
        raise last

    def post(self, path: str, json: Any) -> dict:
        return self.request("POST", path, json=json)

    def get(self, path: str) -> dict:
        return self.request("GET", path)


def download(url: str, dest, timeout: float = 300.0) -> None:
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
