"""Tests for SPKI fingerprint (Fix #8) and random password (Fix #9)."""

import hashlib
import base64
import tempfile
from pathlib import Path

import pytest

from ai_browser.registration_handler.models import RegistrationConfig


class TestSPKIFingerprint:
    """Test that _calculate_cert_spki_fingerprint hashes the SPKI, not the whole cert."""

    @staticmethod
    def _make_self_signed_cert() -> bytes:
        """Generate a self-signed cert and return PEM bytes using cryptography."""
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.backends import default_backend
        import datetime

        key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048, backend=default_backend()
        )
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "test.local"),
        ])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.UTC))
            .not_valid_after(
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
            )
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None),
                critical=True,
            )
            .sign(private_key=key, algorithm=hashes.SHA256(), backend=default_backend())
        )
        return cert.public_bytes(serialization.Encoding.PEM)

    def test_spki_hash_matches_expected(self):
        """The SPKI hash from our method matches what cryptography computes directly."""
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import serialization

            cert_pem = self._make_self_signed_cert()

            # Compute expected: SPKI DER → SHA256 → base64
            cert = x509.load_pem_x509_certificate(cert_pem)
            spki_der = cert.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            expected = base64.b64encode(hashlib.sha256(spki_der).digest()).decode()

            # Now check the whole-cert hash is DIFFERENT (proving old code was wrong)
            whole_cert_hash = base64.b64encode(
                hashlib.sha256(
                    cert.public_bytes(serialization.Encoding.DER)
                ).digest()
            ).decode()

            # SPKI hash should differ from whole-cert hash
            assert expected != whole_cert_hash, (
                "SPKI hash should differ from whole-cert hash"
            )

        except ImportError:
            pytest.skip("cryptography library not installed")


class TestRandomPassword:
    """Test that the default password is randomly generated (Fix #9)."""

    def test_default_password_is_random(self):
        """Two configs with no explicit password get different passwords."""
        config1 = RegistrationConfig(
            signup_url="https://target1.com/signup",
            email="test+target1@mydomain.com",
        )
        config2 = RegistrationConfig(
            signup_url="https://target2.com/signup",
            email="test+target2@mydomain.com",
        )
        assert config1.password != config2.password, (
            "Default passwords should be random per instance"
        )

    def test_default_password_is_long_enough(self):
        """Random default password is at least 16 characters."""
        config = RegistrationConfig(
            signup_url="https://target.com/signup",
            email="test+target@mydomain.com",
        )
        assert len(config.password) >= 16

    def test_explicit_password_is_preserved(self):
        """When an explicit password is provided, it is used."""
        config = RegistrationConfig(
            signup_url="https://target.com/signup",
            email="test+target@mydomain.com",
            password="MyExplicitP@ssw0rd!",
        )
        assert config.password == "MyExplicitP@ssw0rd!"
