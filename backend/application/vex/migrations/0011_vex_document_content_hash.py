from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vex", "0010_cyclonedx_cyclonedx_branch_cyclonedx_vulnerability"),
    ]

    operations = [
        migrations.AddField(
            model_name="vex_document",
            name="content_hash",
            field=models.CharField(blank=True, default="", max_length=256),
        ),
    ]
