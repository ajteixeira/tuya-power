"""Cliente mínimo da Tuya Cloud OpenAPI (assinatura HMAC-SHA256, token em cache)."""
import asyncio
import hashlib
import hmac
import json
import time
import uuid

import httpx

from .config import settings


class TuyaError(Exception):
    pass


class TuyaClient:
    def __init__(self):
        self._token = ""
        self._expires = 0.0
        self._http = httpx.AsyncClient(base_url=settings.tuya_endpoint, timeout=20)

    def _sign(self, method, path_qs, body, token):
        t = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        content = hashlib.sha256((body or "").encode()).hexdigest()
        string_to_sign = f"{method}\n{content}\n\n{path_qs}"
        msg = settings.tuya_access_id + token + t + nonce + string_to_sign
        sign = hmac.new(settings.tuya_access_secret.encode(), msg.encode(),
                        hashlib.sha256).hexdigest().upper()
        headers = {"client_id": settings.tuya_access_id, "sign": sign, "t": t,
                   "sign_method": "HMAC-SHA256", "nonce": nonce}
        if token:
            headers["access_token"] = token
        if body:
            headers["Content-Type"] = "application/json"
        return headers

    async def _ensure_token(self):
        if self._token and time.time() < self._expires - 60:
            return
        path = "/v1.0/token?grant_type=1"
        r = await self._http.get(path, headers=self._sign("GET", path, "", ""))
        data = r.json()
        if not data.get("success"):
            raise TuyaError(f"Token: {data.get('msg')} ({data.get('code')})")
        res = data["result"]
        self._token = res["access_token"]
        self._expires = time.time() + res["expire_time"]

    async def request(self, method, path, params=None, body=None):
        if not settings.tuya_access_id:
            raise TuyaError("Credenciais Tuya não configuradas (.env).")
        await self._ensure_token()
        if params:
            qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v not in (None, ""))
            if qs:
                path = f"{path}?{qs}"
        raw = json.dumps(body, separators=(",", ":")) if body is not None else ""
        for attempt in range(6):
            r = await self._http.request(method, path, content=raw or None,
                                         headers=self._sign(method, path, raw, self._token))
            data = r.json()
            # 40000309: limite de frequência -> esperar e repetir
            if data.get("code") == 40000309 and attempt < 5:
                await asyncio.sleep(5 * 2 ** attempt)
                continue
            break
        if not data.get("success"):
            raise TuyaError(f"{data.get('msg')} ({data.get('code')})")
        return data.get("result")

    # --- API de alto nível ---
    async def list_devices(self):
        out, row_key = [], ""
        while True:
            res = await self.request("GET", "/v1.0/iot-01/associated-users/devices",
                                     {"size": 100, "last_row_key": row_key})
            out += res.get("devices", [])
            if not res.get("has_more"):
                return out
            row_key = res.get("last_row_key", "")

    async def status(self, dev_id):
        return await self.request("GET", f"/v1.0/devices/{dev_id}/status")

    async def info(self, dev_id):
        return await self.request("GET", f"/v1.0/devices/{dev_id}")

    async def specifications(self, dev_id):
        return await self.request("GET", f"/v1.0/devices/{dev_id}/specifications")

    async def send(self, dev_id, commands):
        return await self.request("POST", f"/v1.0/devices/{dev_id}/commands",
                                  body={"commands": commands})

    async def logs(self, dev_id, start_ms, end_ms, row_key=""):
        return await self.request("GET", f"/v1.0/devices/{dev_id}/logs", {
            "type": "1,2,3,4,5,6,7,8,9,10", "start_time": start_ms, "end_time": end_ms,
            "query_type": 1, "size": 100, "start_row_key": row_key})



tuya = TuyaClient()
