"""Wrappers around the official validators (Java, CI and optional sidecar only).

* KoSIT validator + XRechnung configuration: XRechnung CII/UBL business rules
* Mustang CLI: ZUGFeRD/Factur-X PDF incl. PDF/A-3 (veraPDF) and XML schematron

The jars are fetched by ``tools/validators/fetch.sh``; ``SKONTIO_VALIDATORS_DIR`` overrides
the location.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

_REPO_ROOT = Path(__file__).resolve().parents[3]
TIMEOUT_SECONDS = 300


class ValidatorUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    summary: str
    report: bytes


def validators_dir() -> Path:
    return Path(os.environ.get("SKONTIO_VALIDATORS_DIR", _REPO_ROOT / "tools" / "validators" / ".cache"))


def _java() -> str:
    java = shutil.which("java")
    if not java:
        raise ValidatorUnavailable("Java ist nicht installiert")
    return java


def _require(path: Path) -> Path:
    if not path.exists():
        raise ValidatorUnavailable(f"{path} fehlt – bitte tools/validators/fetch.sh ausführen")
    return path


def validate_with_kosit(xml: bytes, *, workdir: Path) -> ValidationResult:
    base = validators_dir()
    jar = _require(base / "kosit-validator.jar")
    config = _require(base / "xrechnung-config")
    source = workdir / "invoice.xml"
    source.write_bytes(xml)
    out = workdir / "kosit"
    out.mkdir(exist_ok=True)
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            _java(),
            "-jar",
            str(jar),
            "-s",
            str(config / "scenarios.xml"),
            "-r",
            str(config),
            "-o",
            str(out),
            str(source),
        ],
        capture_output=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    reports = list(out.glob("*-report.xml"))
    if not reports:
        raise ValidatorUnavailable("KoSIT hat keinen Prüfbericht erzeugt")
    report = reports[0].read_bytes()
    root = etree.fromstring(report)
    ns = {"rep": "http://www.xoev.de/de/validator/varl/1", "svrl": "http://purl.oclc.org/dsdl/svrl"}
    accepted = bool(root.xpath("//rep:assessment/rep:accept", namespaces=ns))
    messages = [
        " ".join(text.split())
        for text in root.xpath("//rep:message[@level='error' or @level='warning']/text()", namespaces=ns)
    ]
    return ValidationResult(accepted, "\n".join(messages) or ("accept" if accepted else "reject"), report)


def validate_with_mustang(pdf: bytes, *, workdir: Path) -> ValidationResult:
    jar = _require(validators_dir() / "mustang-cli.jar")
    source = workdir / "invoice.pdf"
    source.write_bytes(pdf)
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [_java(), "-jar", str(jar), "--action", "validate", "--source", str(source), "--no-notices"],
        capture_output=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
        cwd=workdir,
    )
    output = completed.stdout
    match = re.search(rb"<validation.*</validation>", output, re.DOTALL)
    if not match:
        raise ValidatorUnavailable("Mustang lieferte keinen Prüfbericht: " + completed.stderr.decode(errors="replace"))
    report = match.group(0)
    root = etree.fromstring(report)
    status = root.xpath("string(/validation/summary/@status)")
    errors = [" ".join(e.xpath("string()").split()) for e in root.xpath("//error")]
    return ValidationResult(status == "valid", "\n".join(errors) or status, report)
