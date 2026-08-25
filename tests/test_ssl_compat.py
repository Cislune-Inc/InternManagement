import os
import ssl
from types import SimpleNamespace

import certifi

from agent import ssl_compat
from agent.ssl_compat import build_ssl_context, ensure_ssl_cert_file


def test_ensure_ssl_cert_file_leaves_native_truststore_enabled(monkeypatch) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    ensure_ssl_cert_file()

    assert "SSL_CERT_FILE" not in os.environ


def test_ensure_ssl_cert_file_preserves_existing_value(monkeypatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/custom-cert.pem")

    ensure_ssl_cert_file()

    assert os.environ["SSL_CERT_FILE"] == "/tmp/custom-cert.pem"


def test_ensure_ssl_cert_file_preserves_ssl_cert_dir_override(monkeypatch) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("SSL_CERT_DIR", "/tmp/custom-certs")

    ensure_ssl_cert_file()

    assert "SSL_CERT_FILE" not in os.environ


def test_build_ssl_context_requires_cert_verification() -> None:
    context = build_ssl_context()

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_build_ssl_context_prefers_ssl_cert_file(monkeypatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/custom-cert.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "/tmp/custom-certs")
    captured: dict[str, str | None] = {}
    sentinel = object()

    def fake_create_default_context(*, cafile=None, capath=None):
        captured["cafile"] = cafile
        captured["capath"] = capath
        return sentinel

    monkeypatch.setattr(ssl_compat.ssl, "create_default_context", fake_create_default_context)

    assert build_ssl_context() is sentinel
    assert captured == {"cafile": "/tmp/custom-cert.pem", "capath": None}


def test_build_ssl_context_honors_ssl_cert_dir(monkeypatch) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("SSL_CERT_DIR", "/tmp/custom-certs")
    captured: dict[str, str | None] = {}
    sentinel = object()

    def fake_create_default_context(*, cafile=None, capath=None):
        captured["cafile"] = cafile
        captured["capath"] = capath
        return sentinel

    monkeypatch.setattr(ssl_compat.ssl, "create_default_context", fake_create_default_context)

    assert build_ssl_context() is sentinel
    assert captured == {"cafile": None, "capath": "/tmp/custom-certs"}


def test_build_ssl_context_uses_certifi_when_env_missing(monkeypatch) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(ssl_compat, "truststore", None)
    captured: dict[str, str | None] = {}
    sentinel = object()

    def fake_create_default_context(*, cafile=None, capath=None):
        captured["cafile"] = cafile
        captured["capath"] = capath
        return sentinel

    monkeypatch.setattr(ssl_compat.ssl, "create_default_context", fake_create_default_context)

    assert build_ssl_context() is sentinel
    assert captured == {"cafile": certifi.where(), "capath": None}


def test_build_ssl_context_uses_native_truststore_when_available(monkeypatch) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    captured: dict[str, int] = {}
    sentinel = object()

    def fake_ssl_context(protocol):
        captured["protocol"] = protocol
        return sentinel

    monkeypatch.setattr(
        ssl_compat,
        "truststore",
        SimpleNamespace(SSLContext=fake_ssl_context),
    )

    assert build_ssl_context() is sentinel
    assert captured == {"protocol": ssl.PROTOCOL_TLS_CLIENT}
