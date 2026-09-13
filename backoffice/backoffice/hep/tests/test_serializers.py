import json
import pytest

from django.contrib.auth.models import Group
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from unittest.mock import patch

from backoffice.hep.api.serializers import (
    HepBackofficeSearchUISerializer,
    HepWorkflowSerializer,
)
from backoffice.hep.constants import HepStatusChoices, HepWorkflowType
from backoffice.users.tests.factories import UserFactory
from django.apps import apps

HepWorkflow = apps.get_model(app_label="hep", model_name="HepWorkflow")


class TestHepBackofficeSearchUISerializer(SimpleTestCase):
    def test_hep_serializer_includes_hep_fields(self):
        payload = {
            "count": 1,
            "results": [
                {
                    "id": "workflow-id",
                    "legacy_creation_date": "2026-03-10T10:00:00",
                    "_created_at": "2026-03-10T11:00:00",
                    "_updated_at": "2026-03-10T12:00:00",
                    "data": {"titles": [{"title": "Title"}]},
                    "decisions": [],
                    "workflow_type": "create",
                    "status": "running",
                    "classifier_results": {"score": 0.9},
                    "matches": [{"control_number": 123}],
                    "relevance_prediction": "high",
                    "reference_count": 42,
                }
            ],
        }

        data = HepBackofficeSearchUISerializer(payload).data

        hit = data["hits"]["hits"][0]
        self.assertEqual(hit["classifier_results"], {"score": 0.9})
        self.assertEqual(hit["matches"], [{"control_number": 123}])
        self.assertEqual(hit["relevance_prediction"], "high")
        self.assertEqual(hit["reference_count"], 42)


class TestHepWorkflowSerializer(TestCase):
    def test_create_sets_source_data_from_data(self):
        payload = {
            "workflow_type": HepWorkflowType.HEP_CREATE,
            "status": HepStatusChoices.RUNNING,
            "data": {
                "_collections": ["Literature"],
                "document_type": ["article"],
                "titles": [{"title": "Original title"}],
            },
        }

        serializer = HepWorkflowSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        workflow = serializer.save()
        workflow.refresh_from_db()

        self.assertEqual(workflow.data, payload["data"])
        self.assertEqual(workflow.source_data, payload["data"])

    def test_create_persists_form_data(self):
        payload = {
            "workflow_type": HepWorkflowType.HEP_CREATE,
            "status": HepStatusChoices.RUNNING,
            "data": {
                "_collections": ["Literature"],
                "document_type": ["article"],
                "titles": [{"title": "Original title"}],
            },
            "form_data": {
                "references": "[1] First line\\n[2] Second line",
                "url": "https://example.org",
            },
        }

        serializer = HepWorkflowSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        workflow = serializer.save()
        workflow.refresh_from_db()

        self.assertEqual(workflow.form_data, payload["form_data"])

    def test_serializer_returns_null_form_data_when_missing(self):
        workflow = HepWorkflow.objects.create(
            workflow_type=HepWorkflowType.HEP_CREATE,
            status=HepStatusChoices.RUNNING,
            data={},
            form_data=None,
        )

        serialized = HepWorkflowSerializer(workflow).data

        self.assertIsNone(serialized["form_data"])

    def test_serializer_leaves_form_data_unchanged_without_references(self):
        workflow = HepWorkflow.objects.create(
            workflow_type=HepWorkflowType.HEP_CREATE,
            status=HepStatusChoices.RUNNING,
            data={},
            form_data={"url": "https://example.org"},
        )

        serialized = HepWorkflowSerializer(workflow).data

        self.assertEqual(serialized["form_data"], {"url": "https://example.org"})

    def test_serializer_preserves_non_string_references(self):
        workflow = HepWorkflow.objects.create(
            workflow_type=HepWorkflowType.HEP_CREATE,
            status=HepStatusChoices.RUNNING,
            data={},
            form_data={
                "references": ["[1] First line", "[2] Second line"],
                "url": "https://example.org",
            },
        )

        serialized = HepWorkflowSerializer(workflow).data

        self.assertEqual(
            serialized["form_data"]["references"],
            ["[1] First line", "[2] Second line"],
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "references",
    [
        "[1] Müller, α decay\n[2] 李, 𝛽 decay",
        r"[1] Study of $\alpha$, $\nu$ and $\times$",
        r"[1] Literal \n and \u03b1 in a title",
        "[1] Actual newline\n[2] Actual tab\tend",
        "[1] Backslash before Greek \\α and Chinese \\李",
        "[1] A\\\\B\n[2] Trailing backslash \\",
        r"[1] Incomplete escapes \x and \u123",
    ],
)
@override_settings(ALLOWED_HOSTS=["testserver"])
def test_submission_references_survive_json_api_round_trips(references):
    user = UserFactory()
    group, _ = Group.objects.get_or_create(name="curator")
    user.groups.add(group)
    client = APIClient()
    client.force_authenticate(user=user)
    payload = {
        "workflow_type": HepWorkflowType.HEP_SUBMISSION,
        "status": HepStatusChoices.RUNNING,
        "data": {
            "_collections": ["Literature"],
            "document_type": ["article"],
            "titles": [{"title": "Reference preservation"}],
        },
        "form_data": {"references": references, "url": "https://example.org"},
    }

    # Exercise real JSON parsing: wire escapes are decoded once by the parser.
    with patch("backoffice.hep.api.views.trigger_hep_workflow_initialization.delay"):
        created = client.post(
            reverse("api:hep-list"),
            json.dumps(payload, ensure_ascii=True),
            content_type="application/json",
        )
    assert created.status_code == 201, created.data
    workflow = HepWorkflow.objects.get(pk=created.json()["id"])
    assert workflow.form_data == payload["form_data"]
    assert created.json()["form_data"] == payload["form_data"]

    url = reverse("api:hep-detail", kwargs={"pk": workflow.pk})
    for _ in range(2):
        retrieved = client.get(url)
        assert retrieved.status_code == 200
        assert retrieved.json()["form_data"] == payload["form_data"]
        updated = client.put(url, retrieved.json(), format="json")
        assert updated.status_code == 200, updated.data
        workflow.refresh_from_db()
        assert workflow.form_data == payload["form_data"]
        assert updated.json()["form_data"] == payload["form_data"]
