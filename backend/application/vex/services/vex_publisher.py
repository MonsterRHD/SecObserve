"""Revision ordered, transactional publishing of parsed VEX documents.

The publisher is the only component that persists imported VEX data. It makes
sure that:

* older revisions never overwrite a newer one,
* identical replays of the same revision are idempotent,
* the same revision with different content is rejected,
* a newer revision is published in a single atomic transaction so that parsing
  failures, database errors or a killed process always leave the previous
  revision intact,
* concurrent imports of the same document identity converge to the highest
  confirmed revision.
"""

from enum import Enum

from django.db import IntegrityError, connection, transaction
from rest_framework.exceptions import ValidationError

from application.core.models import Branch, Observation, Product
from application.vex.models import VEX_Document, VEX_Statement
from application.vex.services.vex_candidate import (
    ParsedVEXDocument,
    calculate_content_hash,
    compare_revisions,
)
from application.vex.services.vex_engine import (
    get_search_purl,
    apply_vex_statement_with_previous,
    find_vex_statement_for_observation,
    statements_semantically_equal,
    write_observation_log_no_vex_statement,
)


class _Decision(Enum):
    IDEMPOTENT = "idempotent"
    REPLACE = "replace"


def publish_vex_document(candidate: ParsedVEXDocument) -> None:
    """Publish a fully validated candidate according to its revision order."""
    content_hash = calculate_content_hash(candidate)

    with transaction.atomic():
        existing = _get_locked_document(candidate)

        if existing is not None:
            decision = _decide_for_existing(existing, candidate, content_hash)
            if decision == _Decision.IDEMPOTENT:
                return
            _replace_document(existing, candidate, content_hash)
            return

        # The document identity does not exist yet. Create the row in a nested
        # savepoint so that a concurrent insert losing the unique constraint
        # race can re-evaluate against the winner instead of failing. Up to two
        # losing rounds are handled in case more than two imports race.
        document = None
        for _attempt in range(2):
            try:
                with transaction.atomic():
                    document = _create_document(candidate, content_hash)
                break
            except IntegrityError:
                existing = _get_locked_document(candidate)
                if existing is None:
                    # The competing transaction rolled back, retry the insert.
                    continue
                decision = _decide_for_existing(existing, candidate, content_hash)
                if decision == _Decision.IDEMPOTENT:
                    return
                _replace_document(existing, candidate, content_hash)
                return

        if document is None:
            # Two unique constraint races with rolling back winners: give up
            # and let the caller retry instead of corrupting any state.
            raise IntegrityError("Could not create VEX document after concurrent insert conflicts")

        _insert_statements_and_apply(document, candidate, frozenset())


def _decide_for_existing(existing: VEX_Document, candidate: ParsedVEXDocument, content_hash: str) -> _Decision:
    if existing.type != candidate.type:
        raise ValidationError(
            f"VEX document {candidate.document_id} already exists with a different document type "
            f"({existing.type}), uploaded type is {candidate.type}"
        )

    relation = compare_revisions(candidate, existing.version)

    if relation == "older":
        raise ValidationError(
            f"VEX document {candidate.document_id} (version {candidate.version}) is older than the "
            f"already published revision (version {existing.version}), import rejected"
        )

    if relation == "equal":
        if existing.content_hash == content_hash:
            return _Decision.IDEMPOTENT
        if existing.content_hash == "":
            # Document created before content hashes existed: perform one
            # silent replacement to backfill the hash.
            return _Decision.REPLACE
        raise ValidationError(
            f"VEX document {candidate.document_id} (version {candidate.version}) already exists with "
            "different content, increment the version to publish a new revision"
        )

    return _Decision.REPLACE


def _replace_document(document: VEX_Document, candidate: ParsedVEXDocument, content_hash: str) -> None:
    document.type = candidate.type
    document.version = candidate.version
    document.initial_release_date = candidate.initial_release_date
    document.current_release_date = candidate.current_release_date
    document.role = candidate.role
    document.content_hash = content_hash
    document.save()

    old_statement_ids = frozenset(VEX_Statement.objects.filter(document=document).values_list("pk", flat=True))

    _insert_statements_and_apply(document, candidate, old_statement_ids)


def _create_document(candidate: ParsedVEXDocument, content_hash: str) -> VEX_Document:
    return VEX_Document.objects.create(
        type=candidate.type,
        document_id=candidate.document_id,
        version=candidate.version,
        initial_release_date=candidate.initial_release_date,
        current_release_date=candidate.current_release_date,
        author=candidate.author,
        role=candidate.role,
        content_hash=content_hash,
    )


def _insert_statements_and_apply(
    document: VEX_Document,
    candidate: ParsedVEXDocument,
    old_statement_ids: frozenset[int],
) -> None:
    new_statements = [
        VEX_Statement(
            document=document,
            vulnerability_id=statement.vulnerability_id,
            description=statement.description,
            status=statement.status,
            justification=statement.justification,
            impact=statement.impact,
            remediation=statement.remediation,
            product_purl=statement.product_purl,
            component_purl=statement.component_purl,
            component_cyclonedx_bom_link=statement.component_cyclonedx_bom_link,
        )
        for statement in candidate.statements
    ]
    VEX_Statement.objects.bulk_create(new_statements)

    affected_observation_ids = _collect_affected_observation_ids(candidate, new_statements, old_statement_ids)

    observations = (
        Observation.objects.filter(pk__in=affected_observation_ids)
        .select_related("product", "branch", "vex_statement__document")
        .order_by("pk")
    )
    for observation in observations:
        previous_vex_statement = observation.vex_statement
        chosen = find_vex_statement_for_observation(observation, new_statements)

        if chosen is not None:
            statement_identity_ignored = (
                previous_vex_statement is not None
                and previous_vex_statement.pk in old_statement_ids
                and statements_semantically_equal(previous_vex_statement, chosen)
            )
            apply_vex_statement_with_previous(
                observation,
                chosen,
                previous_vex_statement,
                statement_identity_ignored=statement_identity_ignored,
            )
        elif previous_vex_statement is not None and previous_vex_statement.pk in old_statement_ids:
            # The previous revision claimed the observation but the new one
            # does not: revert the VEX fields and log the removal.
            write_observation_log_no_vex_statement(observation, previous_vex_statement)

    if old_statement_ids:
        # All observations have been repointed/reverted above, so the pre_delete
        # signal finds no linked observations and writes no additional logs.
        VEX_Statement.objects.filter(pk__in=old_statement_ids).delete()


def _collect_affected_observation_ids(
    candidate: ParsedVEXDocument,
    new_statements: list[VEX_Statement],
    old_statement_ids: frozenset[int],
) -> set[int]:
    affected: set[int] = set()

    # Observations currently linked to statements of the previous revision.
    affected.update(Observation.objects.filter(vex_statement_id__in=old_statement_ids).values_list("pk", flat=True))

    # Statements with CycloneDX BOM links.
    bom_links = {
        statement.component_cyclonedx_bom_link for statement in new_statements if statement.component_cyclonedx_bom_link
    }
    if bom_links:
        affected.update(
            Observation.objects.filter(origin_component_cyclonedx_bom_link__in=bom_links).values_list("pk", flat=True)
        )

    # Products/branches identified by the document's product PURLs.
    for product_purl in candidate.product_purls:
        search_purl = get_search_purl(product_purl)
        if not search_purl:
            continue

        product_ids = set(Product.objects.filter(purl__startswith=search_purl).values_list("pk", flat=True))
        product_ids.update(Branch.objects.filter(purl__startswith=search_purl).values_list("product_id", flat=True))
        if product_ids:
            affected.update(Observation.objects.filter(product_id__in=product_ids).values_list("pk", flat=True))

    # Product-only statements matched by the observation's component PURL.
    for statement in new_statements:
        if statement.product_purl and not statement.component_purl:
            search_purl = get_search_purl(statement.product_purl)
            if not search_purl:
                continue
            affected.update(
                Observation.objects.filter(origin_component_purl__startswith=search_purl).values_list("pk", flat=True)
            )

    return affected


def _get_locked_document(candidate: ParsedVEXDocument) -> VEX_Document | None:
    queryset = VEX_Document.objects.filter(document_id=candidate.document_id, author=candidate.author)
    if connection.features.has_select_for_update:
        queryset = queryset.select_for_update()
    return queryset.first()
