"""S3 DocumentSource with httpx and a hand-written AWS Signature V4 (no boto3).

Config: bucket, region, prefix, endpoint (optional; path-style, for S3-compatible stores),
access_key_ref, secret_key_ref (key names; values read at call time and never stored).
Changes come from ListObjectsV2 with the cursor = newest LastModified seen. Bodies are read with a
Range request for classification and discarded; hashes are computed by streaming. The durable link
is `s3://bucket/key` (no presigned URL, which would expire and embed a signature).
"""

from __future__ import annotations

import hashlib
import hmac
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from gw.harness.contracts import DocChange, DocMeta, ExternalRef, Health, SyncResult
from gw.harness.registry import register, resolve_config, secret
from gw.record import models as m

NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(method: str, host: str, path: str, query: dict[str, str], region: str, access_key: str,
                  secret_key: str, now: datetime, extra: dict[str, str] | None = None, service: str = "s3") -> dict[str, str]:
    """AWS SigV4 headers for a request with an empty body (all S3 calls here are reads)."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    headers = {"host": host, "x-amz-content-sha256": EMPTY_SHA, "x-amz-date": amz_date, **{k.lower(): v for k, v in (extra or {}).items()}}
    canon_query = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(query.items()))
    signed = ";".join(sorted(headers))
    canon_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers))
    canonical = "\n".join([method, quote(path, safe="/-_.~"), canon_query, canon_headers, signed, EMPTY_SHA])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), day), region), service), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k2: v for k2, v in headers.items() if k2 != "host"}
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return out


@register("s3")
class S3Documents:
    def __init__(self, conn: m.Connection, transport: httpx.BaseTransport | None = None,
                 clock: Any = None) -> None:
        self.conn = conn
        cfg = resolve_config(conn.config)
        self.bucket = str(cfg.get("bucket", ""))
        self.region = str(cfg.get("region", "eu-central-1"))
        self.prefix = str(cfg.get("prefix", ""))
        endpoint = cfg.get("endpoint")
        if endpoint:  # path-style for S3-compatible stores
            self.base = str(endpoint).rstrip("/")
            self.host = httpx.URL(self.base).host
            self.path_prefix = f"/{self.bucket}"
        else:
            self.host = f"{self.bucket}.s3.{self.region}.amazonaws.com"
            self.base = f"https://{self.host}"
            self.path_prefix = ""
        self.ak_ref, self.sk_ref = cfg.get("access_key_ref"), cfg.get("secret_key_ref")
        self.client = httpx.Client(base_url=self.base, timeout=60, transport=transport)
        self.clock = clock or (lambda: datetime.now(UTC))

    def _request(self, method: str, key: str = "", query: dict[str, str] | None = None,
                 extra: dict[str, str] | None = None) -> httpx.Response:
        path = f"{self.path_prefix}/{key}" if key else (self.path_prefix or "/")
        headers = sigv4_headers(method, self.host or "", path, query or {}, self.region, secret(self.ak_ref) or "",
                                secret(self.sk_ref) or "", self.clock(), extra)
        r = self.client.request(method, quote(path, safe="/-_.~"), params=query, headers=headers)
        r.raise_for_status()
        return r

    def list_changes(self, cursor: str | None) -> tuple[list[DocChange], str]:
        since = datetime.fromisoformat(cursor) if cursor else None
        changes: list[DocChange] = []
        newest = since
        token: str | None = None
        while True:
            q = {"list-type": "2", "prefix": self.prefix}
            if token:
                q["continuation-token"] = token
            root = ET.fromstring(self._request("GET", query=q).text)
            for c in root.findall(f"{NS}Contents"):
                key = c.findtext(f"{NS}Key") or ""
                mod = datetime.fromisoformat((c.findtext(f"{NS}LastModified") or "").replace("Z", "+00:00"))
                if key.endswith("/") or (since and mod <= since):
                    continue
                changes.append(DocChange(ref=ExternalRef(conn_id=self.conn.id, external_id=key,
                                                         url=f"s3://{self.bucket}/{key}"),
                                         name=key.rsplit("/", 1)[-1], modified=mod))
                newest = max(newest, mod) if newest else mod
            if (root.findtext(f"{NS}IsTruncated") or "false") != "true":
                break
            token = root.findtext(f"{NS}NextContinuationToken")
        return changes, (newest or self.clock()).isoformat()

    def stat(self, ref: ExternalRef) -> DocMeta:
        h = self._request("HEAD", ref.external_id).headers
        mod = h.get("last-modified")
        from email.utils import parsedate_to_datetime

        return DocMeta(name=ref.external_id.rsplit("/", 1)[-1], size=int(h.get("content-length", 0)),
                       modified=parsedate_to_datetime(mod) if mod else self.clock(),
                       version=h.get("x-amz-version-id") or h.get("etag"))

    def read_text(self, ref: ExternalRef, max_bytes: int) -> str:
        r = self._request("GET", ref.external_id, extra={"Range": f"bytes=0-{max_bytes - 1}"})
        return r.content[:max_bytes].decode("utf-8", errors="replace")

    def hash(self, ref: ExternalRef) -> str:
        path = f"{self.path_prefix}/{ref.external_id}"
        headers = sigv4_headers("GET", self.host or "", path, {}, self.region, secret(self.ak_ref) or "",
                                secret(self.sk_ref) or "", self.clock())
        h = hashlib.sha256()
        with self.client.stream("GET", quote(path, safe="/-_.~"), headers=headers) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(1 << 16):
                h.update(chunk)
        return h.hexdigest()

    def link(self, ref: ExternalRef) -> str:
        return f"s3://{self.bucket}/{ref.external_id}"

    def sync(self, since: str | None) -> SyncResult:
        ch, cur = self.list_changes(since)
        return SyncResult(pulled=len(ch), changed=len(ch), cursor=cur)

    def health(self) -> Health:
        try:
            self._request("GET", query={"list-type": "2", "prefix": self.prefix, "max-keys": "1"})
            return Health(status="ok", detail=f"s3://{self.bucket}/{self.prefix}")
        except Exception as e:
            return Health(status="down", detail=str(e)[:200])
