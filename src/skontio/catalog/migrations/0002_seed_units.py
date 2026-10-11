from django.db import migrations

# UN/ECE Recommendation 20 codes commonly used by freelancers (allowed in XRechnung code list)
UNITS = [
    ("C62", "Stück", "piece"),
    ("HUR", "Stunde", "hour"),
    ("MIN", "Minute", "minute"),
    ("DAY", "Tag", "day"),
    ("WEE", "Woche", "week"),
    ("MON", "Monat", "month"),
    ("ANN", "Jahr", "year"),
    ("LS", "Pauschale", "lump sum"),
    ("KMT", "Kilometer", "kilometre"),
    ("KGM", "Kilogramm", "kilogram"),
    ("MTR", "Meter", "metre"),
    ("MTK", "Quadratmeter", "square metre"),
    ("LTR", "Liter", "litre"),
]


def seed(apps, schema_editor):
    Unit = apps.get_model("catalog", "Unit")
    for code, label_de, label_en in UNITS:
        Unit.objects.update_or_create(code=code, defaults={"label_de": label_de, "label_en": label_en})


def unseed(apps, schema_editor):
    apps.get_model("catalog", "Unit").objects.filter(code__in=[u[0] for u in UNITS]).delete()


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
