import csv
import re

from django.core.management.base import BaseCommand

from recipe_db.etl.loader import RecipeLoader
from recipe_db.models import Recipe

SOURCE = "brewersfriend"
URL_ID = re.compile(r"/view/(\d+)/")

# Plausible value ranges (mirroring the analyze UI bounds). Values outside these
# are treated as bad data and dropped (set None) rather than skewing histograms.
SG_MIN, SG_MAX = 0.99, 1.20        # specific gravity
PLATO_MIN, PLATO_MAX = 2.0, 40.0   # degrees Plato
ABV_MAX = 21.0
IBU_MAX = 300.0
SRM_MAX = 100.0


def num(value):
    if value is None:
        return None
    value = value.strip()
    if value == "" or value.upper() == "N/A":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def bounded(value, lo, hi):
    return value if (value is not None and lo <= value <= hi) else None


class Command(BaseCommand):
    help = "Import a Brewer's Friend recipe CSV (e.g. the Kaggle dataset) into the database."

    def add_arguments(self, parser):
        parser.add_argument("file_path", help="Path to recipeData.csv")
        parser.add_argument("--replace", action="store_true", help="Re-import ids already in the DB")
        parser.add_argument("--limit", type=int, default=0, help="Stop after N imports (0 = all)")

    def handle(self, *args, **options):
        path = options["file_path"]
        replace = options["replace"]
        limit = options["limit"]
        loader = RecipeLoader()

        imported = skipped = bad = 0
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, row in enumerate(csv.DictReader(f)):
                if limit and imported >= limit:
                    break

                # The real Brewer's Friend recipe id lives in the URL, not BeerID
                # (which is just the export row index). It drives the source link.
                match = URL_ID.search(row.get("URL") or "")
                rid = match.group(1) if match else (row.get("BeerID") or "").strip()
                if not rid:
                    bad += 1
                    continue

                uid = "%s:%s" % (SOURCE, rid)
                if not replace and Recipe.objects.filter(pk=uid).exists():
                    skipped += 1
                    continue

                recipe = Recipe()
                recipe.uid = uid
                recipe.source = SOURCE
                recipe.source_id = rid
                recipe.name = (row.get("Name") or "").strip() or None
                recipe.style_raw = (row.get("Style") or "").strip() or None

                # Gravity scale is per-row. Set the canonical field; Recipe.save()
                # derives the counterpart (plato<->gravity).
                og = num(row.get("OG"))
                fg = num(row.get("FG"))
                if (row.get("SugarScale") or "").strip() == "Plato":
                    recipe.original_plato = bounded(og, PLATO_MIN, PLATO_MAX)
                    recipe.final_plato = bounded(fg, PLATO_MIN, PLATO_MAX)
                else:  # "Specific Gravity"
                    recipe.og = bounded(og, SG_MIN, SG_MAX)
                    recipe.fg = bounded(fg, SG_MIN, SG_MAX)

                recipe.abv = bounded(num(row.get("ABV")), 0, ABV_MAX)
                recipe.ibu = bounded(num(row.get("IBU")), 0, IBU_MAX)
                recipe.srm = bounded(num(row.get("Color")), 0, SRM_MAX)   # Color is SRM; save() -> ebc
                recipe.extract_efficiency = bounded(num(row.get("Efficiency")), 0, 100)
                boil = num(row.get("BoilTime"))
                recipe.boiling_time = int(boil) if boil is not None and boil >= 0 else None

                try:
                    loader.validate_and_fix_recipe(recipe)  # nulls any field failing validation
                    recipe.save()                            # derives plato/gravity/ebc, fills abv
                    imported += 1
                except Exception as e:
                    bad += 1
                    if bad <= 5:
                        self.stderr.write("row %d (%s): %s" % (i, rid, repr(e)[:140]))

                if imported and imported % 5000 == 0:
                    self.stdout.write("  ... imported=%d skipped=%d bad=%d" % (imported, skipped, bad))

        self.stdout.write("Done. imported=%d skipped=%d bad=%d" % (imported, skipped, bad))
