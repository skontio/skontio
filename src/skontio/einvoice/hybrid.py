"""ZUGFeRD 2 / Factur-X hybrid PDF: PDF/A-3 plus embedded CII XML (``factur-x.xml``)."""

from __future__ import annotations

import facturx


def embed_xml(pdf_a3: bytes, cii_xml: bytes, *, lang: str = "de-DE") -> bytes:
    """Attach the XML with AFRelationship ``Data`` and write the Factur-X XMP extension schema."""
    return facturx.generate_from_binary(
        pdf_a3,
        cii_xml,
        flavor="factur-x",
        level="en16931",
        check_xsd=True,
        lang=lang,
        afrelationship="data",
    )
