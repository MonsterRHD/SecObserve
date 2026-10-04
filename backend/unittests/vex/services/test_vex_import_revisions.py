import copy
import io
import json
from os import path
from unittest.mock import patch

from django.db import IntegrityError
from rest_framework.exceptions import ValidationError

from application.core.models import Observation, Observation_Log, Product
from application.core.types import Status
from application.vex.models import VEX_Document, VEX_Statement
from application.vex.services import vex_publisher
from application.vex.services.vex_import import import_vex
from application.vex.types import VEX_Document_Type
from unittests.vex.services.basetest_vex_import import BaseTestVEXImport

PRODUCT_PURL = "pkg:github/SecObserve/VEX_Test@v1.7.0"
CRYPTOGRAPHY_PURL = "pkg:pypi/cryptography@41.0.5"
CRYPTOGRAPHY_BOM_LINK = "urn:cdx:aebccdfe-fab4-4210-acce-bac771d4842d/1#pkg:pypi/cryptography@41.0.5"

OPENVEX_DOCUMENT_ID = "https://openvex.example.com/test_revisions"
CYCLONEDX_SERIAL = "urn:uuid:test-revisions"


def _import_dict(document: dict) -> None:
    import_vex(io.BytesIO(json.dumps(document).encode("utf-8")))


def _openvex_statement(
    vulnerability: str,
    status: str,
    *,
    product_purl: str = PRODUCT_PURL,
    component_purl: str = CRYPTOGRAPHY_PURL,
    justification: str = "",
    impact: str = "",
    action: str = "",
) -> dict:
    product: dict = {"identifiers": {"purl": product_purl}}
    if component_purl:
        product["subcomponents"] = [{"identifiers": {"purl": component_purl}}]
    statement: dict = {
        "vulnerability": {"name": vulnerability},
        "status": status,
        "products": [product],
    }
    if justification:
        statement["justification"] = justification
    if impact:
        statement["impact_statement"] = impact
    if action:
        statement["action_statement"] = action
    return statement


def _openvex_document(
    version, statements, *, document_id=OPENVEX_DOCUMENT_ID, timestamp="2024-01-01T00:00:00Z", last_updated=None
) -> dict:
    return {
        "@context": "https://openvex.dev/ns/v0.2.0",
        "@id": document_id,
        "author": "SecObserve",
        "version": version,
        "timestamp": timestamp,
        "last_updated": last_updated or timestamp,
        "role": "vendor",
        "statements": statements,
    }


def _openvex_v1() -> dict:
    return _openvex_document(
        1,
        [
            _openvex_statement(
                "CVE-2023-49083",
                "not_affected",
                justification="vulnerable_code_cannot_be_controlled_by_adversary",
                impact="Not affected in revision 1",
            )
        ],
    )


def _openvex_v2() -> dict:
    return _openvex_document(
        2,
        [
            _openvex_statement(
                "CVE-2023-49083",
                "affected",
                action="Upgrade cryptography to version 42.0.2",
            )
        ],
        timestamp="2024-01-01T00:00:00Z",
        last_updated="2024-02-01T00:00:00Z",
    )


def _openvex_v3() -> dict:
    # No statement for CVE-2023-49083 anymore, a different vulnerability is assessed.
    return _openvex_document(
        3,
        [
            _openvex_statement(
                "CVE-2024-0727",
                "affected",
                action="Upgrade cryptography to version 42.0.2",
            )
        ],
        timestamp="2024-01-01T00:00:00Z",
        last_updated="2024-03-01T00:00:00Z",
    )


def _cyclonedx_document(version, state, *, timestamp="2024-01-01T00:00:00Z", include_timestamp=True) -> dict:
    metadata: dict = {"authors": [{"name": "SecObserve"}]}
    if include_timestamp:
        metadata["timestamp"] = timestamp
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": CYCLONEDX_SERIAL,
        "version": version,
        "metadata": metadata,
        "vulnerabilities": [
            {
                "id": "CVE-2023-49083",
                "description": "cryptography vulnerability",
                "analysis": {
                    "state": state,
                    "justification": "requires_configuration" if state == "not_affected" else "",
                    "detail": "Not affected in revision 1" if state == "not_affected" else "",
                },
                "affects": [{"ref": CRYPTOGRAPHY_BOM_LINK}],
            }
        ],
    }


class TestVEXImportRevisions(BaseTestVEXImport):
    def setUp(self):
        super().setUp()
        self.load_vex_test()
        self.product = Product.objects.get(purl=PRODUCT_PURL)
        self.logs_baseline = self._log_count()

    def _observation(
        self,
        vulnerability_id: str = "CVE-2023-49083",
        component_purl: str = CRYPTOGRAPHY_PURL,
    ) -> Observation:
        return Observation.objects.get(
            product=self.product,
            vulnerability_id=vulnerability_id,
            origin_component_purl=component_purl,
        )

    def _log_count(self) -> int:
        return Observation_Log.objects.filter(observation__product=self.product).count()

    def _document(self, document_id: str = OPENVEX_DOCUMENT_ID) -> VEX_Document:
        return VEX_Document.objects.get(document_id=document_id, author="SecObserve")

    def _statement_pks(self, document: VEX_Document) -> set[int]:
        return set(VEX_Statement.objects.filter(document=document).values_list("pk", flat=True))

    # --- OpenVEX ---------------------------------------------------------

    def test_openvex_new_import_publishes_revision_1(self):
        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            _import_dict(_openvex_v1())

        observation = self._observation()
        self.assertEqual(Status.STATUS_NOT_AFFECTED, observation.vex_status)
        self.assertEqual(Status.STATUS_NOT_AFFECTED, observation.current_status)
        self.assertIsNotNone(observation.vex_statement)
        self.assertEqual(1, mock_push.call_count)

        document = self._document()
        self.assertEqual("1", document.version)
        self.assertEqual(1, VEX_Statement.objects.filter(document=document).count())
        self.assertEqual(self.logs_baseline + 1, self._log_count())

    def test_openvex_idempotent_replay(self):
        _import_dict(_openvex_v1())
        document = self._document()
        statement_pks = self._statement_pks(document)
        observation = self._observation()
        statement_pk = observation.vex_statement_id
        logs_before = self._log_count()

        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            # Re-dumped JSON has a different byte layout but identical semantics.
            _import_dict(json.loads(json.dumps(_openvex_v1(), indent=4)))

        document.refresh_from_db()
        observation.refresh_from_db()
        self.assertEqual(statement_pks, self._statement_pks(document))
        self.assertEqual(statement_pk, observation.vex_statement_id)
        self.assertEqual(logs_before, self._log_count())
        self.assertEqual(0, mock_push.call_count)

    def test_openvex_same_revision_different_content_rejected(self):
        _import_dict(_openvex_v1())
        document = self._document()
        statement_pks = self._statement_pks(document)
        logs_before = self._log_count()

        conflicting = _openvex_v1()
        conflicting["statements"][0]["impact_statement"] = "Changed impact for same revision"

        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            with self.assertRaises(ValidationError):
                _import_dict(conflicting)

        document.refresh_from_db()
        self.assertEqual(statement_pks, self._statement_pks(document))
        self.assertEqual(logs_before, self._log_count())
        self.assertEqual(0, mock_push.call_count)
        observation = self._observation()
        self.assertEqual("Not affected in revision 1", observation.vex_statement.impact)

    def test_openvex_older_revision_rejected(self):
        _import_dict(_openvex_v2())
        observation = self._observation()
        self.assertEqual(Status.STATUS_AFFECTED, observation.vex_status)
        statement_pk = observation.vex_statement_id
        document = self._document()
        statement_pks = self._statement_pks(document)
        logs_before = self._log_count()

        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            with self.assertRaises(ValidationError):
                _import_dict(_openvex_v1())

        document.refresh_from_db()
        observation.refresh_from_db()
        self.assertEqual("2", document.version)
        self.assertEqual(statement_pks, self._statement_pks(document))
        self.assertEqual(statement_pk, observation.vex_statement_id)
        self.assertEqual(Status.STATUS_AFFECTED, observation.vex_status)
        self.assertEqual(logs_before, self._log_count())
        self.assertEqual(0, mock_push.call_count)

    def test_openvex_newer_revision_replaces_atomically(self):
        _import_dict(_openvex_v1())
        document = self._document()
        old_statement_pks = self._statement_pks(document)
        observation = self._observation()
        old_observation_statement_pk = observation.vex_statement_id
        self.assertIn(old_observation_statement_pk, old_statement_pks)
        logs_before = self._log_count()

        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            _import_dict(_openvex_v2())

        document.refresh_from_db()
        self.assertEqual("2", document.version)
        self.assertNotEqual("", document.content_hash)
        new_statement_pks = self._statement_pks(document)
        self.assertEqual(1, len(new_statement_pks))
        self.assertTrue(new_statement_pks.isdisjoint(old_statement_pks))

        observation.refresh_from_db()
        self.assertEqual(Status.STATUS_AFFECTED, observation.vex_status)
        self.assertEqual(Status.STATUS_AFFECTED, observation.current_status)
        self.assertIn(observation.vex_statement_id, new_statement_pks)
        self.assertEqual(1, mock_push.call_count)
        self.assertEqual(logs_before + 1, self._log_count())

        # A removed VEX statement is logged exactly once (no removal/addition pair).
        latest_log = Observation_Log.objects.filter(observation=observation).order_by("-pk").first()
        self.assertIn("Updated by VEX statement", latest_log.comment)

    def test_openvex_revision_removing_statement_reverts_observation(self):
        _import_dict(_openvex_v1())
        logs_before_v3 = self._log_count()
        logs_49083_before = Observation_Log.objects.filter(observation=self._observation()).count()

        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            _import_dict(_openvex_v3())

        # Only the newly applied CVE-2024-0727 statement triggers a push; the
        # removed CVE-2023-49083 statement must not.
        self.assertEqual(1, mock_push.call_count)

        document = self._document()
        self.assertEqual("3", document.version)
        self.assertEqual(1, VEX_Statement.objects.filter(document=document).count())
        self.assertEqual(
            0, VEX_Statement.objects.filter(document=document, vulnerability_id="CVE-2023-49083").count()
        )

        observation_49083 = self._observation()
        self.assertIsNone(observation_49083.vex_statement)
        self.assertEqual("", observation_49083.vex_status)

        # The dropped statement creates exactly one removal log, even though the
        # statement row is deleted in the same transaction (no signal double log).
        logs_49083_after = Observation_Log.objects.filter(observation=observation_49083).count()
        self.assertEqual(logs_49083_before + 1, logs_49083_after)
        removal_log = (
            Observation_Log.objects.filter(observation=observation_49083).order_by("-pk").first()
        )
        self.assertEqual(
            f"Removed VEX statement from {OPENVEX_DOCUMENT_ID}",
            removal_log.comment,
        )

        observation_0727 = self._observation("CVE-2024-0727")
        self.assertEqual(Status.STATUS_AFFECTED, observation_0727.vex_status)
        self.assertIsNotNone(observation_0727.vex_statement)

        # One log for the dropped observation, one for the newly applied one.
        self.assertEqual(logs_before_v3 + 2, self._log_count())

    def test_openvex_composite_revision_logs_each_observation_at_most_once(self):
        sqlparse_purl = "pkg:pypi/sqlparse@0.4.4"
        gunicorn_purl = "pkg:pypi/gunicorn@21.2.0"
        v1 = _openvex_document(
            1,
            [
                _openvex_statement(
                    "CVE-2023-49083",
                    "not_affected",
                    justification="vulnerable_code_cannot_be_controlled_by_adversary",
                    impact="Not affected in revision 1",
                ),
                _openvex_statement("CVE-2024-0727", "affected", action="Upgrade cryptography to 41.0.0"),
                _openvex_statement(
                    "CVE-2024-4340",
                    "under_investigation",
                    component_purl=sqlparse_purl,
                ),
            ],
        )
        v2 = _openvex_document(
            2,
            [
                # Unchanged statement: repoint silently without a log.
                _openvex_statement(
                    "CVE-2023-49083",
                    "not_affected",
                    justification="vulnerable_code_cannot_be_controlled_by_adversary",
                    impact="Not affected in revision 1",
                ),
                # Changed statement: exactly one log and one push.
                _openvex_statement("CVE-2024-0727", "fixed"),
                # Newly added statement: first application, one log and one push.
                _openvex_statement("CVE-2024-1135", "affected", action="Upgrade gunicorn", component_purl=gunicorn_purl),
                # CVE-2024-4340 statement is dropped in revision 2.
            ],
            last_updated="2024-02-01T00:00:00Z",
        )
        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker"):
            _import_dict(v1)
        document = self._document()
        old_statement_pks = self._statement_pks(document)
        obs_49083 = self._observation()
        obs_0727 = self._observation("CVE-2024-0727")
        obs_4340 = self._observation("CVE-2024-4340", sqlparse_purl)
        obs_1135 = self._observation("CVE-2024-1135", gunicorn_purl)
        logs_before = self._log_count()
        per_obs_before = {
            obs.pk: Observation_Log.objects.filter(observation=obs).count()
            for obs in (obs_49083, obs_0727, obs_4340, obs_1135)
        }

        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            _import_dict(v2)

        document.refresh_from_db()
        new_statement_pks = self._statement_pks(document)
        self.assertEqual(3, len(new_statement_pks))
        self.assertTrue(new_statement_pks.isdisjoint(old_statement_pks))
        # Pushes only for the changed and the newly applied statements, not for
        # the silently repointed one or the dropped one.
        self.assertEqual(2, mock_push.call_count)

        obs_49083.refresh_from_db()
        # Silent repoint: same semantic result, new statement row, no new log.
        self.assertEqual(Status.STATUS_NOT_AFFECTED, obs_49083.vex_status)
        self.assertIn(obs_49083.vex_statement_id, new_statement_pks)
        self.assertEqual(
            per_obs_before[obs_49083.pk],
            Observation_Log.objects.filter(observation=obs_49083).count(),
        )

        obs_0727.refresh_from_db()
        self.assertEqual(Status.STATUS_RESOLVED, obs_0727.vex_status)
        self.assertEqual(
            per_obs_before[obs_0727.pk] + 1,
            Observation_Log.objects.filter(observation=obs_0727).count(),
        )

        obs_4340.refresh_from_db()
        self.assertIsNone(obs_4340.vex_statement)
        self.assertEqual("", obs_4340.vex_status)
        self.assertEqual(
            per_obs_before[obs_4340.pk] + 1,
            Observation_Log.objects.filter(observation=obs_4340).count(),
        )

        obs_1135.refresh_from_db()
        self.assertEqual(Status.STATUS_AFFECTED, obs_1135.vex_status)
        self.assertIn(obs_1135.vex_statement_id, new_statement_pks)
        self.assertEqual(
            per_obs_before[obs_1135.pk] + 1,
            Observation_Log.objects.filter(observation=obs_1135).count(),
        )

        # 0 (unchanged) + 1 (changed) + 1 (dropped) + 1 (added) = 3 new logs.
        self.assertEqual(logs_before + 3, self._log_count())

    def test_openvex_out_of_order_converges_to_highest(self):
        _import_dict(_openvex_v1())
        _import_dict(_openvex_v3())

        with self.assertRaises(ValidationError):
            _import_dict(_openvex_v2())

        document = self._document()
        self.assertEqual("3", document.version)
        self.assertEqual(1, VEX_Statement.objects.filter(document=document).count())
        observation = self._observation()
        self.assertIsNone(observation.vex_statement)
        self.assertEqual("", observation.vex_status)

    def test_openvex_parse_failure_keeps_previous_revision(self):
        _import_dict(_openvex_v1())
        document = self._document()
        statement_pks = self._statement_pks(document)
        logs_before = self._log_count()

        invalid = _openvex_v2()
        del invalid["statements"][0]["status"]

        with self.assertRaises(ValidationError):
            _import_dict(invalid)

        document.refresh_from_db()
        self.assertEqual("1", document.version)
        self.assertEqual(statement_pks, self._statement_pks(document))
        self.assertEqual(logs_before, self._log_count())
        observation = self._observation()
        self.assertEqual(Status.STATUS_NOT_AFFECTED, observation.vex_status)

    def test_openvex_database_error_rolls_back_replace(self):
        _import_dict(_openvex_v1())
        document = self._document()
        statement_pks = self._statement_pks(document)
        observation = self._observation()
        statement_pk = observation.vex_statement_id
        logs_before = self._log_count()

        with self.assertRaises(Exception):  # noqa: B017, PT027
            with patch.object(VEX_Statement.objects, "bulk_create", side_effect=IntegrityError("injected")):
                _import_dict(_openvex_v2())

        document.refresh_from_db()
        self.assertEqual("1", document.version)
        self.assertEqual(statement_pks, self._statement_pks(document))
        observation.refresh_from_db()
        self.assertEqual(statement_pk, observation.vex_statement_id)
        self.assertEqual(Status.STATUS_NOT_AFFECTED, observation.vex_status)
        self.assertEqual(logs_before, self._log_count())

    def test_openvex_legacy_empty_hash_is_backfilled_silently(self):
        _import_dict(_openvex_v1())
        document = self._document()
        document.content_hash = ""
        document.save()
        logs_before = self._log_count()

        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            _import_dict(_openvex_v1())

        document.refresh_from_db()
        self.assertNotEqual("", document.content_hash)
        observation = self._observation()
        self.assertEqual(Status.STATUS_NOT_AFFECTED, observation.vex_status)
        self.assertEqual(logs_before, self._log_count())
        self.assertEqual(0, mock_push.call_count)

        # The next replay is idempotent with stable statement primary keys.
        statement_pks = self._statement_pks(document)
        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            _import_dict(_openvex_v1())
        self.assertEqual(statement_pks, self._statement_pks(self._document()))
        self.assertEqual(0, mock_push.call_count)

    # --- CycloneDX with BOM links ---------------------------------------

    def test_cyclonedx_bom_link_revision_flow(self):
        v1 = _cyclonedx_document(1, "not_affected")
        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker"):
            _import_dict(v1)

        observation = self._observation()
        self.assertEqual(Status.STATUS_NOT_AFFECTED, observation.vex_status)
        self.assertEqual(CRYPTOGRAPHY_BOM_LINK, observation.vex_statement.component_cyclonedx_bom_link)
        self.assertEqual("", observation.vex_statement.product_purl)
        document = self._document(CYCLONEDX_SERIAL)
        statement_pks = self._statement_pks(document)
        logs_before = self._log_count()

        # Idempotent replay
        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            _import_dict(v1)
        self.assertEqual(statement_pks, self._statement_pks(document))
        self.assertEqual(logs_before, self._log_count())
        self.assertEqual(0, mock_push.call_count)

        # Newer revision changes the assessment to resolved/fixed
        v2 = _cyclonedx_document(2, "resolved", timestamp="2024-02-01T00:00:00Z")
        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            _import_dict(v2)
        observation.refresh_from_db()
        self.assertEqual(Status.STATUS_RESOLVED, observation.vex_status)
        self.assertEqual(Status.STATUS_RESOLVED, observation.current_status)
        self.assertEqual(1, mock_push.call_count)
        self.assertEqual(logs_before + 1, self._log_count())
        self.assertTrue(self._statement_pks(document).isdisjoint(statement_pks))

        # Late older revision is rejected with a full state snapshot
        resolved_statement_pk = observation.vex_statement_id
        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            with self.assertRaises(ValidationError) as context:
                _import_dict(v1)
        self.assertIn(CYCLONEDX_SERIAL, str(context.exception.detail))
        observation.refresh_from_db()
        self.assertEqual(Status.STATUS_RESOLVED, observation.vex_status)
        self.assertEqual(resolved_statement_pk, observation.vex_statement_id)
        self.assertEqual(logs_before + 1, self._log_count())
        self.assertEqual(0, mock_push.call_count)

    def test_cyclonedx_same_revision_different_content_rejected(self):
        v1 = _cyclonedx_document(1, "not_affected")
        _import_dict(v1)
        document = self._document(CYCLONEDX_SERIAL)
        statement_pks = self._statement_pks(document)
        observation = self._observation()
        statement_pk = observation.vex_statement_id
        logs_before = self._log_count()

        conflicting = _cyclonedx_document(1, "not_affected")
        conflicting["vulnerabilities"][0]["analysis"]["detail"] = "Changed justification detail"

        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            with self.assertRaises(ValidationError) as context:
                _import_dict(conflicting)
        self.assertIn(CYCLONEDX_SERIAL, str(context.exception.detail))
        self.assertEqual(statement_pks, self._statement_pks(document))
        observation.refresh_from_db()
        self.assertEqual(statement_pk, observation.vex_statement_id)
        self.assertEqual(Status.STATUS_NOT_AFFECTED, observation.vex_status)
        self.assertEqual(logs_before, self._log_count())
        self.assertEqual(0, mock_push.call_count)

    def test_cyclonedx_document_without_timestamp_is_idempotent(self):
        v1 = _cyclonedx_document(1, "not_affected", include_timestamp=False)
        _import_dict(v1)
        document = self._document(CYCLONEDX_SERIAL)
        statement_pks = self._statement_pks(document)
        logs_before = self._log_count()

        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            _import_dict(copy.deepcopy(v1))

        self.assertEqual(statement_pks, self._statement_pks(document))
        self.assertEqual(logs_before, self._log_count())
        self.assertEqual(0, mock_push.call_count)

    # --- CSAF ------------------------------------------------------------

    def test_csaf_idempotent_older_and_conflict(self):
        filename = path.dirname(__file__) + "/files/so_csaf_2020_0001_0001.json"
        with open(filename) as testfile:
            csaf_v1 = json.load(testfile)

        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker"):
            _import_dict(csaf_v1)

        document = VEX_Document.objects.get(
            document_id="https://csaf.example.com/so_2020_0001_0001", author="SecObserve"
        )
        statement_pks = self._statement_pks(document)
        logs_before = self._log_count()
        observation = self._observation()
        statement_pk = observation.vex_statement_id

        # Idempotent replay from re-dumped JSON
        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            _import_dict(json.loads(json.dumps(csaf_v1)))
        self.assertEqual(statement_pks, self._statement_pks(document))
        self.assertEqual(logs_before, self._log_count())
        self.assertEqual(0, mock_push.call_count)

        # Same version with different current release date is a conflict
        conflict = copy.deepcopy(csaf_v1)
        conflict["document"]["tracking"]["current_release_date"] = "2024-08-14T11:12:19.671919+00:00"
        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            with self.assertRaises(ValidationError) as context:
                _import_dict(conflict)
        self.assertIn(document.document_id, str(context.exception.detail))
        self.assertEqual(statement_pks, self._statement_pks(document))
        observation.refresh_from_db()
        self.assertEqual(statement_pk, observation.vex_statement_id)
        self.assertEqual(logs_before, self._log_count())
        self.assertEqual(0, mock_push.call_count)

        # Older version is rejected with full state snapshot
        older = copy.deepcopy(csaf_v1)
        older["document"]["tracking"]["version"] = "0"
        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            with self.assertRaises(ValidationError) as context:
                _import_dict(older)
        self.assertIn(document.document_id, str(context.exception.detail))
        self.assertEqual("1", document.version)
        self.assertEqual(statement_pks, self._statement_pks(document))
        observation.refresh_from_db()
        self.assertEqual(statement_pk, observation.vex_statement_id)
        self.assertEqual(Status.STATUS_NOT_AFFECTED, observation.vex_status)
        self.assertEqual(logs_before, self._log_count())
        self.assertEqual(0, mock_push.call_count)

    def test_document_with_same_identity_but_different_type_rejected(self):
        _import_dict(_openvex_v1())
        logs_before = self._log_count()
        statement_pks = self._statement_pks(self._document(OPENVEX_DOCUMENT_ID))

        # A CycloneDX document claims the same (document_id, author) identity.
        cross_type = _cyclonedx_document(1, "not_affected")
        cross_type["serialNumber"] = OPENVEX_DOCUMENT_ID

        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            with self.assertRaises(ValidationError) as context:
                _import_dict(cross_type)
        self.assertIn("different document type", str(context.exception.detail))

        # The OpenVEX document is untouched and no CycloneDX document was created.
        self.assertEqual(statement_pks, self._statement_pks(self._document(OPENVEX_DOCUMENT_ID)))
        self.assertEqual(1, VEX_Document.objects.filter(document_id=OPENVEX_DOCUMENT_ID).count())
        self.assertEqual(
            VEX_Document_Type.VEX_DOCUMENT_TYPE_OPENVEX,
            VEX_Document.objects.get(document_id=OPENVEX_DOCUMENT_ID).type,
        )
        self.assertEqual(logs_before, self._log_count())
        self.assertEqual(0, mock_push.call_count)

    # --- Concurrent insert race handling --------------------------------

    def test_unique_race_loser_retakes_idempotent_decision(self):
        _import_dict(_openvex_v1())
        document = self._document()
        statement_pks = self._statement_pks(document)
        logs_before = self._log_count()

        real_get_locked = vex_publisher._get_locked_document
        calls = {"count": 0}

        def flaky_get_locked(candidate):
            calls["count"] += 1
            return None if calls["count"] == 1 else real_get_locked(candidate)

        with patch("application.vex.services.vex_engine.push_observation_to_issue_tracker") as mock_push:
            with patch("application.vex.services.vex_publisher._get_locked_document", side_effect=flaky_get_locked):
                with patch.object(VEX_Document.objects, "create", side_effect=IntegrityError("race")):
                    _import_dict(_openvex_v1())

        self.assertEqual(statement_pks, self._statement_pks(self._document()))
        self.assertEqual(logs_before, self._log_count())
        self.assertEqual(0, mock_push.call_count)

    def test_unique_race_loser_retakes_replace_decision(self):
        _import_dict(_openvex_v1())

        real_get_locked = vex_publisher._get_locked_document
        calls = {"count": 0}

        def flaky_get_locked(candidate):
            calls["count"] += 1
            return None if calls["count"] == 1 else real_get_locked(candidate)

        with patch("application.vex.services.vex_publisher._get_locked_document", side_effect=flaky_get_locked):
            with patch.object(VEX_Document.objects, "create", side_effect=IntegrityError("race")):
                _import_dict(_openvex_v2())

        self.assertEqual("2", self._document().version)
        self.assertEqual(Status.STATUS_AFFECTED, self._observation().vex_status)

    def test_unique_race_loser_retakes_older_decision(self):
        _import_dict(_openvex_v2())

        real_get_locked = vex_publisher._get_locked_document
        calls = {"count": 0}

        def flaky_get_locked(candidate):
            calls["count"] += 1
            return None if calls["count"] == 1 else real_get_locked(candidate)

        with patch("application.vex.services.vex_publisher._get_locked_document", side_effect=flaky_get_locked):
            with patch.object(VEX_Document.objects, "create", side_effect=IntegrityError("race")):
                with self.assertRaises(ValidationError):
                    _import_dict(_openvex_v1())

        self.assertEqual("2", self._document().version)

    def test_unique_race_winner_rollback_loser_creates_document(self):
        real_get_locked = vex_publisher._get_locked_document
        get_calls = {"count": 0}
        create_calls = {"count": 0}
        real_create = VEX_Document.objects.create

        def flaky_get_locked(candidate):
            get_calls["count"] += 1
            if get_calls["count"] == 1:
                return None
            return None if get_calls["count"] == 2 else real_get_locked(candidate)

        def flaky_create(*args, **kwargs):
            create_calls["count"] += 1
            if create_calls["count"] == 1:
                raise IntegrityError("winner rolled back")
            return real_create(*args, **kwargs)

        with patch("application.vex.services.vex_publisher._get_locked_document", side_effect=flaky_get_locked):
            with patch.object(VEX_Document.objects, "create", side_effect=flaky_create):
                _import_dict(_openvex_v1())

        self.assertEqual("1", self._document().version)
        self.assertEqual(Status.STATUS_NOT_AFFECTED, self._observation().vex_status)

    def test_unique_race_two_losses_then_committed_winner_is_used(self):
        _import_dict(_openvex_v1())
        document = self._document()
        statement_pks = self._statement_pks(document)
        logs_before = self._log_count()

        real_get_locked = vex_publisher._get_locked_document
        calls = {"count": 0}

        def flaky_get_locked(candidate):
            calls["count"] += 1
            # The two in-loop re-reads after losing inserts still miss the row;
            # the committed winner becomes visible on the third lookup.
            if calls["count"] <= 2:
                return None
            return real_get_locked(candidate)

        with patch("application.vex.services.vex_publisher._get_locked_document", side_effect=flaky_get_locked):
            with patch.object(VEX_Document.objects, "create", side_effect=IntegrityError("race")):
                _import_dict(_openvex_v1())

        self.assertEqual(statement_pks, self._statement_pks(self._document()))
        self.assertEqual(logs_before, self._log_count())
