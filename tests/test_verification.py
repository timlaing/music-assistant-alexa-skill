"""Exercise signature and timezone checks; only synthetic chain trust is mocked."""

import base64
from datetime import datetime, timedelta, timezone

import pytest
from ask_sdk_webservice_support.verifier import RequestVerifier, VerificationException
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID


def certificate(expired=False):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "echo-api.amazon.com")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=-1 if expired else 1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("echo-api.amazon.com")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def test_signature_valid_and_tampered_body(monkeypatch):
    verifier = RequestVerifier()
    key, cert = certificate()
    pem = cert.public_bytes(serialization.Encoding.PEM)
    monkeypatch.setattr(verifier, "_load_cert_chain", lambda url: pem)
    # Our synthetic test certificate is not an Amazon-trusted root.
    monkeypatch.setattr(verifier, "_validate_cert_chain", lambda chain: None)
    body = '{"request":{"type":"LaunchRequest"}}'
    signature = key.sign(body.encode(), padding.PKCS1v15(), hashes.SHA256())
    headers = {
        "SignatureCertChainUrl": "https://s3.amazonaws.com/echo.api/test.pem",
        "Signature-256": base64.b64encode(signature).decode(),
    }
    verifier.verify(headers, body, None)
    with pytest.raises(VerificationException):
        verifier.verify(headers, body + " ", None)


def test_expired_certificate_rejected():
    _, cert = certificate(expired=True)
    with pytest.raises(VerificationException):
        RequestVerifier()._validate_end_certificate(cert)


@pytest.mark.parametrize(
    "url",
    [
        "http://s3.amazonaws.com/echo.api/test.pem",
        "https://notamazon.com/echo.api/test.pem",
        "https://s3.amazonaws.com/other/test.pem",
    ],
)
def test_untrusted_certificate_url_rejected(url):
    with pytest.raises(VerificationException):
        RequestVerifier()._validate_certificate_url(url)
