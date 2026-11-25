from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0009_legal_form_onboarding"),
    ]

    operations = [
        migrations.AddField(
            model_name="company",
            name="ui_theme",
            field=models.CharField(
                choices=[("standard", "Standard"), ("modern", "Modern")],
                default="standard",
                max_length=10,
                verbose_name="Oberfläche",
            ),
        ),
    ]
