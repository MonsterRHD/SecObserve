from dataclasses import dataclass
from typing import Optional

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from application.core.api.serializers_helpers import validate_purl
from application.vex.services.vex_candidate import (
    ParsedVEXDocument,
    StatementData,
    parse_release_date,
)
from application.vex.types import (
    CycloneDX_Analysis_State,
    VEX_Document_Type,
    VEX_Status,
)


@dataclass
class CycloneDX_Analysis:
    state: str = ""
    justification: str = ""
    response: Optional[list[str]] = None
    detail: str = ""
    first_issued: str = ""
    last_updated: str = ""


def extract_cyclonedx_document(data: dict) -> ParsedVEXDocument:
    """Validate a CycloneDX VEX document and build an in-memory candidate.

    No database objects are created or modified here.
    """
    cyclonedx_document = _extract_document_metadata(data)

    product_purls, vex_statements = _process_vex_statements(data)

    cyclonedx_document.statements = vex_statements
    cyclonedx_document.product_purls = product_purls

    return cyclonedx_document


def _extract_document_metadata(data: dict) -> ParsedVEXDocument:
    document_id = data.get("serialNumber")
    if not document_id:
        raise ValidationError("serialNumber is missing")

    version = str(data.get("version", 1))

    metadata = data.get("metadata", {})

    timestamp_raw = metadata.get("timestamp")
    if timestamp_raw:
        timestamp = parse_release_date(timestamp_raw, "metadata/timestamp")
        has_source_release_dates = True
    else:
        # Keep the historic server side default for the stored field, but exclude
        # the generated timestamp from the hash so that repeated uploads of the
        # same file without timestamp remain idempotent.
        timestamp = timezone.now()
        has_source_release_dates = False

    author = None
    # Prefer authors list if available
    authors = metadata.get("authors")
    if authors and isinstance(authors, list) and len(authors) > 0:
        # Find the first author with a name set
        author = next(
            (item.get("name") for item in authors if isinstance(item, dict) and item.get("name")),
            None,
        )

    # Fall back to manufacturer or supplier if no authors
    if not author:
        author = metadata.get("manufacturer", {}).get("name") or metadata.get("supplier", {}).get("name")

    if not author:
        author = "Unknown"

    return ParsedVEXDocument(
        type=VEX_Document_Type.VEX_DOCUMENT_TYPE_CYCLONEDX,
        document_id=document_id,
        version=version,
        initial_release_date=timestamp,
        current_release_date=timestamp,
        author=author,
        role="",
        has_source_release_dates=has_source_release_dates,
    )


def _process_vex_statements(data: dict) -> tuple[set[str], list[StatementData]]:
    vulnerabilities = data.get("vulnerabilities", [])
    if not vulnerabilities:
        raise ValidationError("CycloneDX document doesn't contain any vulnerabilities")
    if not isinstance(vulnerabilities, list):
        raise ValidationError("vulnerabilities is not a list")

    components_map = _build_components_map(data)

    product_purl = data.get("metadata", {}).get("component", {}).get("purl", "")
    if product_purl:
        validate_purl(product_purl)

    product_purls: set[str] = set()
    vex_statements: list[StatementData] = []

    vulnerability_counter = 0
    for vulnerability in vulnerabilities:
        if not isinstance(vulnerability, dict):
            raise ValidationError(f"vulnerability[{vulnerability_counter}] is not a dictionary")

        vulnerability_id = vulnerability.get("id")
        if not vulnerability_id:
            raise ValidationError(f"vulnerability[{vulnerability_counter}]/id is missing")

        analysis = vulnerability.get("analysis", {})
        if not analysis:
            # Skip vulnerabilities without analysis
            vulnerability_counter += 1
            continue

        cyclonedx_analysis = _parse_analysis(analysis, vulnerability_counter)

        vex_status = _map_cyclonedx_state_to_vex_status(cyclonedx_analysis.state)
        if not vex_status:
            raise ValidationError(
                f"vulnerability[{vulnerability_counter}]/analysis/state is not valid: {cyclonedx_analysis.state}"
            )

        description = vulnerability.get("description", "")
        detail = vulnerability.get("detail", "")
        if detail:
            description += f"\n\n{detail}"

        remediation = _build_remediation_text(cyclonedx_analysis.response, vulnerability.get("recommendation", ""))

        affects = vulnerability.get("affects", [])
        if not affects:
            # General statement for the product
            _append_statement(
                product_purls,
                vex_statements,
                data=StatementData(
                    vulnerability_id=vulnerability_id,
                    description=description,
                    status=vex_status,
                    justification=cyclonedx_analysis.justification,
                    impact=cyclonedx_analysis.detail,
                    remediation=remediation,
                    product_purl=product_purl,
                ),
            )
        elif not isinstance(affects, list):
            raise ValidationError(f"affects[{vulnerability_counter}] is not a list")
        else:
            _process_affected_components(
                product_purls=product_purls,
                vex_statements=vex_statements,
                vulnerability_counter=vulnerability_counter,
                vex_data=StatementData(
                    vulnerability_id=vulnerability_id,
                    description=description,
                    status=vex_status,
                    justification=cyclonedx_analysis.justification,
                    impact=cyclonedx_analysis.detail,
                    remediation=remediation,
                    product_purl=product_purl,
                ),
                affects=affects,
                components_map=components_map,
            )

        vulnerability_counter += 1

    return product_purls, vex_statements


def _build_components_map(data: dict) -> dict[str, dict]:
    components_map = {}

    # Add root component from metadata
    metadata_component = data.get("metadata", {}).get("component")
    if metadata_component and metadata_component.get("bom-ref"):
        components_map[metadata_component["bom-ref"]] = metadata_component

    # Add all components
    for component in data.get("components", []):
        if component.get("bom-ref"):
            components_map[component["bom-ref"]] = component

    return components_map


def _parse_analysis(analysis: dict, vulnerability_counter: int) -> CycloneDX_Analysis:
    state = analysis.get("state", "")
    if not state:
        raise ValidationError(f"vulnerability[{vulnerability_counter}]/analysis/state is missing")

    justification = analysis.get("justification", "")
    response = analysis.get("response", [])
    if not isinstance(response, list):
        response = []

    detail = analysis.get("detail", "")
    first_issued = analysis.get("firstIssued", "")
    last_updated = analysis.get("lastUpdated", "")

    return CycloneDX_Analysis(
        state=state,
        justification=justification,
        response=response,
        detail=detail,
        first_issued=first_issued,
        last_updated=last_updated,
    )


def _map_cyclonedx_state_to_vex_status(state: str) -> Optional[str]:
    mapping = {
        CycloneDX_Analysis_State.CYCLONEDX_STATE_RESOLVED: VEX_Status.VEX_STATUS_FIXED,
        CycloneDX_Analysis_State.CYCLONEDX_STATE_RESOLVED_WITH_PEDIGREE: VEX_Status.VEX_STATUS_FIXED,
        CycloneDX_Analysis_State.CYCLONEDX_STATE_EXPLOITABLE: VEX_Status.VEX_STATUS_AFFECTED,
        CycloneDX_Analysis_State.CYCLONEDX_STATE_IN_TRIAGE: VEX_Status.VEX_STATUS_UNDER_INVESTIGATION,
        CycloneDX_Analysis_State.CYCLONEDX_STATE_FALSE_POSITIVE: VEX_Status.VEX_STATUS_FALSE_POSITIVE,
        CycloneDX_Analysis_State.CYCLONEDX_STATE_NOT_AFFECTED: VEX_Status.VEX_STATUS_NOT_AFFECTED,
    }
    return mapping.get(state)


def _build_remediation_text(response: Optional[list[str]], recommendation: str) -> str:
    remediation_parts = []

    if response:
        response_text = ", ".join(response)
        remediation_parts.append(f"Response: {response_text}")

    if recommendation:
        remediation_parts.append(recommendation)

    return "; ".join(remediation_parts)


def _process_affected_components(
    *,
    product_purls: set[str],
    vex_statements: list[StatementData],
    vulnerability_counter: int,
    vex_data: StatementData,
    affects: list,
    components_map: dict,
) -> None:
    affected_counter = 0
    for affected in affects:
        if not isinstance(affected, dict):
            raise ValidationError(f"affects[{vulnerability_counter}][{affected_counter}] is not a dictionary")

        ref = affected.get("ref")
        if not ref:
            raise ValidationError(f"affects[{vulnerability_counter}][{affected_counter}]/ref is missing")

        if ref.startswith("urn:cdx:"):
            component_purl = ""
            product_purl = ""
            component_cyclonedx_bom_link = ref
        else:
            component_cyclonedx_bom_link = ""
            component = components_map.get(ref)
            if not component:
                raise ValidationError(
                    f"affects[{vulnerability_counter}][{affected_counter}]/ref '{ref}' not found in components"
                )

            component_purl = component.get("purl", "")
            if not component_purl:
                raise ValidationError(
                    f"affects[{vulnerability_counter}][{affected_counter}]/ref '{ref}' component is missing PURL"
                )
            validate_purl(component_purl)

            if not vex_data.product_purl:
                raise ValidationError("metadata/component/purl is missing for VEX data inside an SBOM")

            product_purl = vex_data.product_purl

        _append_statement(
            product_purls,
            vex_statements,
            data=StatementData(
                vulnerability_id=vex_data.vulnerability_id,
                description=vex_data.description,
                status=vex_data.status,
                justification=vex_data.justification,
                impact=vex_data.impact,
                remediation=vex_data.remediation,
                product_purl=product_purl,
                component_purl=component_purl,
                component_cyclonedx_bom_link=component_cyclonedx_bom_link,
            ),
        )

        affected_counter += 1


def _append_statement(
    product_purls: set[str],
    vex_statements: list[StatementData],
    data: StatementData,
) -> None:
    vex_statements.append(data)
    product_purls.add(data.product_purl)
