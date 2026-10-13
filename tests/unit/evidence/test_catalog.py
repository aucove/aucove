# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Catálogo de TSA del núcleo y piezas comunes (11-trazabilidad §8.7; ``evidence.catalog``).

El catálogo trae FreeTSA y Sectigo con certificados reales bajados de sus fuentes
oficiales; estas pruebas comprueban sin red que cada huella, vigencia y cadena son
coherentes, y que FreeTSA conserva el certificado que venció en marzo de 2026.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx2
import pytest
import truststore
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID

from aucove.evidence import catalog as catalog_mod
from aucove.evidence import tsa
from aucove.evidence.catalog import (
    TOOL_MISSING,
    Check,
    catalog_path,
    classify_http_error,
    clip,
    der_children,
    der_encode,
    der_tlv,
    entry_summary,
    http_client,
    load_catalog,
    parse_catalog,
    read_limited,
    skip_check,
    ssl_context,
)
from tests.conftest import REPO_ROOT

UTC = timezone.utc
# SHA-256 de los archivos PEM que publica FreeTSA en https://freetsa.org/index_en.php.
FREETSA_PUBLISHED = {
    "cacert.pem": "2151b61137ffa86bf664691ba67e7da0b19f98c758e3d228d5d8ebf27e044438",
    "tsa.crt": "8bfb0305bb64e2571ca507552ef3245cb1c2fee8728e0ff8689225081ea13467",
}
EXTRA_MODULES = ("rfc3161_client", "opentimestamps", "bitcoin", "cryptography", "Cryptodome", "httpx2", "truststore")


def _x509(pem: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem.encode("ascii"))


def _utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _raw_catalog() -> dict[str, object]:
    return json.loads(catalog_path().read_text(encoding="utf-8"))


# -- el catálogo empaquetado ------------------------------------------------------------------


def test_packaged_catalog_starts_with_freetsa_and_sectigo_sealing() -> None:
    catalog = load_catalog()
    assert [e.id for e in catalog] == ["freetsa", "sectigo"]
    assert [e.id for e in catalog.sealing()] == ["freetsa", "sectigo"]
    freetsa, sectigo = catalog.entries
    assert freetsa.url == "https://freetsa.org/tsr" and freetsa.policy_oids == ("1.2.3.4.1",)
    assert sectigo.url == "http://timestamp.sectigo.com" and sectigo.policy_oids == ("1.3.6.1.4.1.6449.2.1.1",)
    assert catalog.get("sectigo") is sectigo and catalog.get("digicert") is None
    assert load_catalog() is catalog  # se lee una vez por proceso


def test_catalog_file_is_ascii_with_lf_and_final_newline() -> None:
    assert catalog_path().parts[-2:] == ("_data", "tsa-catalog.json")
    data = catalog_path().read_bytes()
    data.decode("ascii")
    assert b"\r" not in data and data.endswith(b"}\n") and not data.startswith(b"\xef\xbb\xbf")


def test_every_certificate_matches_its_fingerprint_subject_and_validity() -> None:
    for entry in load_catalog():
        for item in (*entry.roots, *entry.certs):
            cert = _x509(item.pem)
            assert item.der() == cert.public_bytes(Encoding.DER)
            assert cert.public_bytes(Encoding.PEM).decode("ascii") == item.pem  # PEM canónico
            assert item.subject == cert.subject.rfc4514_string()
            assert _utc(item.not_before) == cert.not_valid_before_utc
            assert _utc(item.not_after) == cert.not_valid_after_utc


def test_roots_are_self_signed_certification_authorities() -> None:
    for entry in load_catalog():
        for item in entry.roots:
            root = _x509(item.pem)
            assert root.subject == root.issuer
            root.verify_directly_issued_by(root)
            assert root.extensions.get_extension_for_class(x509.BasicConstraints).value.ca


def test_every_tsa_certificate_chains_to_its_root_within_its_validity() -> None:
    for entry in load_catalog():
        roots = [_x509(c.pem) for c in entry.roots]
        pool = [_x509(c.pem) for c in entry.certs]
        leaves = [
            c for c in pool
            if ExtendedKeyUsageOID.TIME_STAMPING in _eku(c) and not _is_ca(c)
        ]
        assert leaves, entry.id
        for leaf in leaves:
            middle = leaf.not_valid_before_utc + (leaf.not_valid_after_utc - leaf.not_valid_before_utc) / 2
            path = tsa.build_path(leaf, pool, roots, middle)
            assert path is not None, (entry.id, leaf.subject.rfc4514_string())
            assert path[0] == leaf and path[-1] in roots


def test_sectigo_chain_goes_through_the_r46_cross_signed_by_usertrust() -> None:
    """La cadena que declara el atributo ESS de Sectigo (y que exige ``openssl ts -verify``)."""
    sectigo = load_catalog().get("sectigo")
    assert sectigo is not None
    root = _x509(sectigo.roots[0].pem)
    assert root.subject.rfc4514_string().startswith("CN=USERTrust RSA Certification Authority")
    assert sectigo.roots[0].sha256 == "e793c9b02fd8aa13e21c31228accb08119643b749c898964b1746d46c3d4cbd2"
    signer = next(_x509(c.pem) for c in sectigo.certs if "Signer R37" in c.subject)
    path = tsa.build_path(signer, [_x509(c.pem) for c in sectigo.certs], [root], datetime(2026, 10, 6, tzinfo=UTC))
    assert path is not None
    assert [c.subject.rfc4514_string().split(",")[0] for c in path] == [
        "CN=Sectigo Public Time Stamping Signer R37",
        "CN=Sectigo Public Time Stamping CA R41",
        "CN=Sectigo Public Time Stamping Root R46",
        "CN=USERTrust RSA Certification Authority",
    ]


def test_freetsa_pems_are_byte_for_byte_the_published_files() -> None:
    """``sha256sum`` de ``root.pem`` coincide con la huella que publica FreeTSA (VERIFY.md)."""
    import hashlib

    freetsa = load_catalog().get("freetsa")
    assert freetsa is not None
    assert hashlib.sha256(freetsa.roots[0].pem.encode("ascii")).hexdigest() == FREETSA_PUBLISHED["cacert.pem"]
    current = next(c for c in freetsa.certs if c.not_after.startswith("2040"))
    assert hashlib.sha256(current.pem.encode("ascii")).hexdigest() == FREETSA_PUBLISHED["tsa.crt"]


def test_freetsa_keeps_the_certificate_that_expired_in_march_2026() -> None:
    """REQ-TRACE-83 y D-45: el certificado vencido sigue en el catálogo y vale en su época."""
    freetsa = load_catalog().get("freetsa")
    assert freetsa is not None
    old = next(c for c in freetsa.certs if c.not_before.startswith("2016"))
    assert old.not_after == "2026-03-11T01:57:39Z"
    leaf, root = _x509(old.pem), _x509(freetsa.roots[0].pem)
    assert tsa.build_path(leaf, [], [root], datetime(2020, 1, 1, tzinfo=UTC)) == [leaf, root]
    assert tsa.build_path(leaf, [], [root], datetime(2026, 10, 6, tzinfo=UTC)) is None


def test_entry_summary_omits_the_pems() -> None:
    summary = entry_summary(load_catalog().entries[0])
    assert summary["id"] == "freetsa" and "pem" not in json.dumps(summary)


# -- validación del archivo -------------------------------------------------------------------


def _mutated(change: object) -> str:
    data = _raw_catalog()
    change(data)  # type: ignore[operator]
    return json.dumps(data)


def _first(data: dict[str, object]) -> dict[str, object]:
    return data["tsa"][0]  # type: ignore[index,no-any-return]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update(version=2), "versión"),
        (lambda d: d["tsa"].append(dict(_first(d))), "repetidos"),
        (lambda d: _first(d).update(id="Free TSA"), "id inválido"),
        (lambda d: _first(d).update(seal="yes"), "booleano"),
        (lambda d: _first(d).update(roots=[]), "al menos una raíz"),
        (lambda d: _first(d).update(policy_oids=["1.2.x"]), "OID"),
        (lambda d: _first(d).update(url="ftp://freetsa.org/tsr"), "URL"),
        (lambda d: _first(d).update(extra=1), "claves esperadas"),
        (lambda d: _first(d)["roots"][0].update(sha256="0" * 64), "no es el SHA-256"),
        (lambda d: _first(d)["roots"][0].update(pem=_first(d)["roots"][0]["pem"].replace("\n", "\r\n")), "canónico"),
        (lambda d: _first(d)["certs"][0].update(not_after="2016-01-01T00:00:00Z"), "vigencia"),
        (lambda d: _first(d)["certs"][0].update(not_before="2016-03-13"), "fecha UTC"),
    ],
)
def test_parse_catalog_rejects_malformed_entries(change: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_catalog(_mutated(change))


def test_parse_catalog_rejects_non_json_and_other_shapes() -> None:
    with pytest.raises(ValueError, match="JSON"):
        parse_catalog("{")
    with pytest.raises(ValueError, match="exactamente"):
        parse_catalog('{"tsa": []}')
    with pytest.raises(ValueError, match="no vacía"):
        parse_catalog('{"version": 1, "tsa": []}')


def test_retired_entries_are_kept_for_verification_but_not_used_to_seal(tmp_path: Path) -> None:
    data = _raw_catalog()
    _first(data)["seal"] = False
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    catalog = load_catalog(path)
    assert [e.id for e in catalog.sealing()] == ["sectigo"]
    assert catalog.get("freetsa") is not None and catalog.get("freetsa").seal is False  # type: ignore[union-attr]


# -- importación perezosa (REQ-ARCH-99) ---------------------------------------------------------


def test_importing_the_evidence_clients_loads_no_extra_module() -> None:
    code = (
        "import sys\n"
        "import aucove.evidence.catalog, aucove.evidence.revocation, aucove.evidence.tsa, aucove.evidence.ots\n"
        f"print(sorted(m for m in {EXTRA_MODULES!r} if m in sys.modules))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout.decode().strip() == "[]"


def test_ci_installs_the_extra_where_the_evidence_tests_run() -> None:
    """Sin el extra, las pruebas de los clientes se omiten: el CI debe instalarlo."""
    import yaml

    ci = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    for job in ("test", "template-ci"):
        syncs = [
            line.strip()
            for step in ci["jobs"][job]["steps"]
            for line in str(step.get("run", "")).splitlines()
            if line.strip().startswith("uv sync")
        ]
        assert syncs, job
        assert all(" --extra evidence " in f" {line} " for line in syncs), (job, syncs)


# -- piezas comunes -----------------------------------------------------------------------------


def test_skip_check_and_clip() -> None:
    check = skip_check("freetsa")
    assert check == Check("skip", "freetsa", "missing_extra", detail="falta el extra evidence", warnings=(TOOL_MISSING,))
    assert TOOL_MISSING == "W_CONFIG_TOOL_MISSING"
    long = clip("a\nb " + "x" * 400)
    assert len(long) == 300 and long.endswith("…") and "\n" not in long
    assert clip("  uno\tdos  ") == "uno dos"


def test_ssl_context_prefers_ssl_cert_file_and_falls_back_to_truststore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    assert isinstance(ssl_context(), truststore.SSLContext)
    bundle = tmp_path / "ca.pem"
    bundle.write_text(load_catalog().entries[0].roots[0].pem, encoding="ascii")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    context = ssl_context()
    assert isinstance(context, ssl.SSLContext) and not isinstance(context, truststore.SSLContext)
    assert any(dict(x[0] for x in c["subject"]).get("organizationName") == "Free TSA" for c in context.get_ca_certs())


def test_http_client_with_a_transport_ignores_proxies_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["User-Agent"])
        return httpx2.Response(200, content=b"ok")

    with http_client(transport=httpx2.MockTransport(handler)) as client:
        assert client.get("https://tsa.example.test/").content == b"ok"
    assert seen and seen[0].endswith("/" + catalog_mod._meta.__version__)


def test_classify_http_error_follows_req_arch_65() -> None:
    request = httpx2.Request("GET", "https://tsa.example.test/")
    tls = httpx2.ConnectError("tls", request=request)
    tls.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
    assert classify_http_error(tls) == "tls"
    assert classify_http_error(httpx2.ConnectError("dns", request=request)) == "offline"
    assert classify_http_error(httpx2.ConnectTimeout("connect", request=request)) == "offline"
    assert classify_http_error(httpx2.ReadTimeout("read", request=request)) == "timeout"
    assert classify_http_error(httpx2.RemoteProtocolError("eof", request=request)) == "http"


def test_read_limited_enforces_the_cap() -> None:
    response = httpx2.Response(200, content=b"x" * 10)
    assert read_limited(response, 10) == b"x" * 10
    with pytest.raises(ValueError, match="más de 9"):
        read_limited(httpx2.Response(200, content=b"x" * 10), 9)


def test_der_helpers_round_trip_and_reject_bad_input() -> None:
    long = der_encode(0x04, b"a" * 300)
    assert long[:4] == b"\x04\x82\x01\x2c"
    outer = der_encode(0x30, der_encode(0x02, b"\x01") + long)
    tag, body, end = der_tlv(outer, 0)
    assert (tag, end) == (0x30, len(outer))
    assert [c[0] for c in der_children(outer, body, end)] == [0x02, 0x04]
    with pytest.raises(ValueError, match="truncado"):
        der_tlv(outer[:-1], 0)
    with pytest.raises(ValueError, match="indefinida"):
        der_tlv(b"\x30\x80\x00\x00", 0)
    with pytest.raises(ValueError, match="varios bytes"):
        der_tlv(b"\x1f\x81\x01\x00", 0)


def _eku(cert: x509.Certificate) -> list[x509.ObjectIdentifier]:
    try:
        return list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)
    except x509.ExtensionNotFound:
        return []


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False
