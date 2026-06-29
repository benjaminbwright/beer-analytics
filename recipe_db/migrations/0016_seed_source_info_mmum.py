from django.db import migrations


# Reference data: maps a recipe's `source` to display name + outbound URL
# templates. Without a SourceInfo row, RecipeResult.url is None and recipe links
# render as "None". recipe_url is .format()-ed with the recipe's source_id.
SOURCES = [
    {
        "source_id": "mmum",
        "name": "Maische Malz und Mehr",
        "icon": "https://www.maischemalzundmehr.de/favicon.ico",
        "page_url": "https://www.maischemalzundmehr.de/",
        "recipe_url": "https://www.maischemalzundmehr.de/index.php?id={}&inhaltmitte=rezept",
    },
]


def seed_sources(apps, schema_editor):
    SourceInfo = apps.get_model("recipe_db", "SourceInfo")
    for src in SOURCES:
        SourceInfo.objects.update_or_create(source_id=src["source_id"], defaults=src)


def unseed_sources(apps, schema_editor):
    SourceInfo = apps.get_model("recipe_db", "SourceInfo")
    SourceInfo.objects.filter(source_id__in=[s["source_id"] for s in SOURCES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("recipe_db", "0015_searchindexupdatequeue"),
    ]

    operations = [
        migrations.RunPython(seed_sources, unseed_sources),
    ]
