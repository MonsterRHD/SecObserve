from django.db import migrations, models
from django.utils import timezone

import application.licenses.models


def create_baseline_generation(apps, schema_editor):
    # All licenses that existed before generations were introduced form the
    # baseline generation #1 and remain the current generation.
    License = apps.get_model("licenses", "License")
    License_Generation = apps.get_model("licenses", "License_Generation")

    generation = License_Generation.objects.create(
        sequence=1,
        status="current",
        trigger="",
        license_count=License.objects.count(),
        checksum="",
        spdx_release_date="",
        error_message="",
        published_at=timezone.now(),
    )

    License.objects.update(generation=generation)


class Migration(migrations.Migration):
    dependencies = [
        ("licenses", "0024_initialize_license_component_components"),
    ]

    operations = [
        migrations.CreateModel(
            name="License_Generation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sequence", models.PositiveIntegerField(unique=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("preparing", "preparing"),
                            ("current", "current"),
                            ("superseded", "superseded"),
                            ("failed", "failed"),
                        ],
                        max_length=16,
                    ),
                ),
                ("trigger", models.CharField(blank=True, default="", max_length=16)),
                ("license_count", models.PositiveIntegerField(default=0)),
                ("checksum", models.CharField(blank=True, default="", max_length=64)),
                ("spdx_release_date", models.CharField(blank=True, default="", max_length=32)),
                ("error_message", models.CharField(blank=True, default="", max_length=2048)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "ordering": ["-sequence"],
            },
        ),
        migrations.AddIndex(
            model_name="license_generation",
            index=models.Index(fields=["status"], name="license_gen_status_idx"),
        ),
        migrations.AddField(
            model_name="license",
            name="generation",
            field=models.ForeignKey(
                null=True,
                on_delete=models.deletion.PROTECT,
                related_name="licenses",
                to="licenses.license_generation",
            ),
        ),
        migrations.AddField(
            model_name="license",
            name="is_custom",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="license",
            name="spdx_id",
            field=models.CharField(max_length=255),
        ),
        migrations.RunPython(code=create_baseline_generation, reverse_code=migrations.RunPython.noop),
        migrations.AlterField(
            model_name="license",
            name="generation",
            field=models.ForeignKey(
                default=application.licenses.models._get_default_license_generation_pk,
                on_delete=models.deletion.PROTECT,
                related_name="licenses",
                to="licenses.license_generation",
            ),
        ),
        migrations.AddConstraint(
            model_name="license",
            constraint=models.UniqueConstraint(
                fields=("spdx_id", "generation"), name="unique_spdx_id_per_generation"
            ),
        ),
    ]
