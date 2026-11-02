from django.db import migrations, models

from skontio.documents.locking import schema_change_operations


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0013_layout_choices"),
    ]

    operations = schema_change_operations(
        migrations.AddField(
            model_name="document",
            name="imported_from",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.AlterField(
            model_name="document",
            name="origin",
            field=models.CharField(
                choices=[
                    ("ui", "Oberfläche"),
                    ("mcp", "MCP"),
                    ("assistant", "KI-Assistent"),
                    ("api", "API"),
                    ("recurring", "Wiederkehrend"),
                    ("import", "Import"),
                ],
                default="ui",
                max_length=16,
            ),
        ),
    )
