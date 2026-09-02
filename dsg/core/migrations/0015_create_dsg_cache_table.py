from django.core.management import call_command
from django.db import migrations


CACHE_TABLE = "dsg_cache_table"
REQUIRED_COLUMNS = {"cache_key", "value", "expires"}


def create_dsg_cache_table(apps, schema_editor):
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        if CACHE_TABLE in connection.introspection.table_names(cursor):
            columns = {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor, CACHE_TABLE
                )
            }
            missing = REQUIRED_COLUMNS - columns
            if missing:
                missing_names = ", ".join(sorted(missing))
                raise RuntimeError(
                    f"Existing {CACHE_TABLE} has the wrong shape; "
                    f"missing required columns: {missing_names}"
                )

    call_command(
        "createcachetable",
        CACHE_TABLE,
        database=connection.alias,
        verbosity=0,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0014_rename_datasetalias_datasettranslation_and_more"),
    ]

    operations = [
        migrations.RunPython(create_dsg_cache_table, migrations.RunPython.noop),
    ]
