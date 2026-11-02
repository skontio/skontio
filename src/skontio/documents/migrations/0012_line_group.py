from django.db import migrations, models

from skontio.documents.locking import schema_change_operations


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0011_gross_unit_price"),
    ]

    operations = schema_change_operations(
        migrations.AddField(
            model_name="documentline",
            name="group",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    )
