"""Add DatasetBucket model, migrate data from DatasetVersion.gcs_bucket, then remove gcs_bucket."""

from django.db import migrations, models
import django.db.models.deletion


def migrate_gcs_buckets_forward(apps, schema_editor):
    """For each DatasetVersion with a non-empty gcs_bucket, create a DatasetBucket and link via M2M."""
    DatasetVersion = apps.get_model("core", "DatasetVersion")
    DatasetBucket = apps.get_model("core", "DatasetBucket")

    for dv in DatasetVersion.objects.exclude(gcs_bucket=""):
        bucket, _ = DatasetBucket.objects.get_or_create(
            dataset=dv.dataset,
            name=dv.gcs_bucket,
        )
        dv.buckets.add(bucket)


def migrate_gcs_buckets_reverse(apps, schema_editor):
    """Reverse: copy first bucket name back to gcs_bucket field."""
    DatasetVersion = apps.get_model("core", "DatasetVersion")

    for dv in DatasetVersion.objects.all():
        first_bucket = dv.buckets.first()
        if first_bucket:
            dv.gcs_bucket = first_bucket.name
            dv.save(update_fields=["gcs_bucket"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0003_alter_grant_source"),
    ]

    operations = [
        # Step 1: Add DatasetBucket model + M2M on DatasetVersion
        migrations.CreateModel(
            name="DatasetBucket",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("dataset", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="buckets", to="core.dataset")),
            ],
            options={
                "db_table": "dataset_bucket",
                "unique_together": {("dataset", "name")},
            },
        ),
        migrations.AddField(
            model_name="datasetversion",
            name="buckets",
            field=models.ManyToManyField(blank=True, related_name="versions", to="core.datasetbucket"),
        ),
        # Step 2: Migrate data
        migrations.RunPython(migrate_gcs_buckets_forward, migrate_gcs_buckets_reverse),
        # Step 3: Remove old field
        migrations.RemoveField(
            model_name="datasetversion",
            name="gcs_bucket",
        ),
    ]
