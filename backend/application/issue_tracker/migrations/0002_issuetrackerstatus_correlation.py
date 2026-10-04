from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("issue_tracker", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="issue_tracker_status",
            name="correlation_id",
            field=models.CharField(blank=True, default="", max_length=64),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="issue_tracker_status",
            name="create_uncertain",
            field=models.BooleanField(default=False),
        ),
        migrations.AddIndex(
            model_name="issue_tracker_status",
            index=models.Index(fields=["correlation_id"], name="issue_track_correl_idx"),
        ),
    ]
