from __future__ import annotations

import os
import ssl

import aiohttp
import certifi

try:
    import truststore
except ImportError:  # pragma: no cover - requirements install it in production
    truststore = None


def ensure_ssl_cert_file() -> None:
    if str(os.environ.get("SSL_CERT_FILE") or "").strip():
        return
    if str(os.environ.get("SSL_CERT_DIR") or "").strip():
        return
    if truststore is None:
        os.environ["SSL_CERT_FILE"] = certifi.where()


def build_ssl_context() -> ssl.SSLContext:
    cert_file = str(os.environ.get("SSL_CERT_FILE") or "").strip()
    if cert_file:
        return ssl.create_default_context(cafile=cert_file)
    cert_dir = str(os.environ.get("SSL_CERT_DIR") or "").strip()
    if cert_dir:
        return ssl.create_default_context(capath=cert_dir)
    if truststore is not None:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return ssl.create_default_context(cafile=certifi.where())


def build_aiohttp_connector() -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(ssl=build_ssl_context())
