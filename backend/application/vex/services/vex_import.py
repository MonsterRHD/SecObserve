from json import load
from typing import Optional

from django.core.files import File
from rest_framework.exceptions import ValidationError

from application.vex.services.csaf_parser import extract_csaf_document
from application.vex.services.cyclonedx_parser import extract_cyclonedx_document
from application.vex.services.openvex_parser import extract_openvex_document
from application.vex.services.vex_publisher import publish_vex_document
from application.vex.types import VEX_Document_Type


def import_vex(vex_file: File) -> None:
    data = _get_json_data(vex_file)
    if not data:
        raise ValidationError("File is not valid JSON")

    vex_type = _get_vex_type(data)
    if not vex_type:
        raise ValidationError("File is not a supported VEX type")

    # Parse and validate the complete candidate before any database write.
    if vex_type == VEX_Document_Type.VEX_DOCUMENT_TYPE_OPENVEX:
        candidate = extract_openvex_document(data)
    elif vex_type == VEX_Document_Type.VEX_DOCUMENT_TYPE_CSAF:
        candidate = extract_csaf_document(data)
    else:
        candidate = extract_cyclonedx_document(data)

    # Publish according to document identity and revision order.
    publish_vex_document(candidate)


def _get_json_data(vex_file: File) -> Optional[dict]:
    try:
        data = load(vex_file)
    except Exception:
        return None

    return data


def _get_vex_type(data: dict) -> Optional[str]:
    if data.get("@context", "").startswith("https://openvex.dev/ns/v0.2.0"):
        return VEX_Document_Type.VEX_DOCUMENT_TYPE_OPENVEX

    if data.get("document", {}).get("category") == "csaf_vex" and data.get("document", {}).get("csaf_version") == "2.0":
        return VEX_Document_Type.VEX_DOCUMENT_TYPE_CSAF

    if data.get("bomFormat") == "CycloneDX":
        return VEX_Document_Type.VEX_DOCUMENT_TYPE_CYCLONEDX

    return None
