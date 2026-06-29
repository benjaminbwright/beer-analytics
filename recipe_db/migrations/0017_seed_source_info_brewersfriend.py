from django.db import migrations


# Brewer's Friend source metadata. recipe_url is .format()-ed with the recipe's
# source_id (the real BF recipe id from the export URL); BF redirects id-only URLs
# to the full slug. Icon is a self-contained base64 SVG beer glass (no external
# fetch, fits SourceInfo.icon max 10240).
SOURCES = [
    {
        "source_id": "brewersfriend",
        "name": "Brewer's Friend",
        "icon": "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxNiAxNiI+PHJlY3QgeD0iMyIgeT0iMyIgd2lkdGg9IjciIGhlaWdodD0iMTEiIHJ4PSIxLjIiIGZpbGw9IiMzZGE1ZDkiLz48cmVjdCB4PSIzIiB5PSIzIiB3aWR0aD0iNyIgaGVpZ2h0PSIzIiByeD0iMS4yIiBmaWxsPSIjZmZmOGU2Ii8+PHBhdGggZD0iTTEwIDVoMi4yQTEuOCAxLjggMCAwIDEgMTQgNi44djIuNEExLjggMS44IDAgMCAxIDEyLjIgMTFIMTB6IiBmaWxsPSJub25lIiBzdHJva2U9IiMzZGE1ZDkiIHN0cm9rZS13aWR0aD0iMS4zIi8+PC9zdmc+Cg==",
        "page_url": "https://www.brewersfriend.com/",
        "recipe_url": "https://www.brewersfriend.com/homebrew/recipe/view/{}/",
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
        ("recipe_db", "0016_seed_source_info_mmum"),
    ]

    operations = [
        migrations.RunPython(seed_sources, unseed_sources),
    ]
