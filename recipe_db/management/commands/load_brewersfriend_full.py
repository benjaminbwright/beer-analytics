import json
import re

from django.core.management.base import BaseCommand

from recipe_db.etl.format.parser import ParserResult, clean_kind
from recipe_db.etl.loader import RecipeLoader
from recipe_db.models import Recipe, RecipeFermentable, RecipeHop, RecipeYeast

SOURCE = "brewersfriend"
URL_ID = re.compile(r"/view/(\d+)/")

# The real Brewer's Friend recipe id lives in the recipe URL, not the JSON key
# (which is just the export row index). It drives the source link + uid, and
# overlaps the ids imported by load_brewersfriend_csv -- so by default this
# command REPLACES those scalar-only stubs with the full ingredient data here.

# --- Brewer's Friend "hops" use strings -> RecipeHop.use -----------------------
# Whirlpool entries look like "Whirlpool                     at 170 F"; matched
# by substring below. Order matters (check "dry hop"/"first wort" before "boil").
HOP_USE_PATTERNS = [
    ("dry hop", RecipeHop.DRY_HOP),
    ("first wort", RecipeHop.FIRST_WORT),
    ("mash", RecipeHop.MASH),
    ("whirlpool", RecipeHop.AROMA),
    ("aroma", RecipeHop.AROMA),
    ("steep", RecipeHop.AROMA),
    ("boil", RecipeHop.BOIL),
]

HOP_FORM = {
    "pellet": RecipeHop.PELLET,
    "plug": RecipeHop.PLUG,
    "leaf": RecipeHop.LEAF,
    "leaf/whole": RecipeHop.LEAF,
    "whole": RecipeHop.LEAF,
}

# Plausible value ranges (mirroring the analyze UI bounds / model validators).
SG_MIN, SG_MAX = 0.99, 1.20
ABV_MAX = 21.0
IBU_MAX = 300.0
SRM_MAX = 100.0


def fnum(value):
    """Float from a JSON number/str, treating the -1 / blank sentinels as None."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    # Brewer's Friend uses -1 for "unknown" (e.g. "ph mash": -1).
    return None if v <= -1 else v


def bounded(value, lo, hi):
    return value if (value is not None and lo <= value <= hi) else None


def parse_hop_use(use_raw):
    s = (use_raw or "").lower()
    for needle, use in HOP_USE_PATTERNS:
        if needle in s:
            return use
    return RecipeHop.BOIL


# The app's own loader caps dry-hop time at 43200 min (30 days) and boil at
# 240 min, so anything beyond 30 days is junk regardless of use. Clamp here
# too: the loader saves hops via add(bulk=False) *before* it runs that cap, so
# a wild value (e.g. "14515200 days") would overflow the INT column first.
TIME_MAX_MINUTES = 43200


def parse_time_minutes(time_raw):
    """'60 min' -> 60, '7 days' -> 10080, '1 hr.' -> 60. Returns int minutes."""
    if time_raw is None:
        return None
    s = str(time_raw).lower()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    n = float(m.group(0))
    if n < 0:
        return None
    if "day" in s:
        minutes = n * 1440
    elif "hr" in s or "hour" in s:
        minutes = n * 60
    elif "sec" in s:
        minutes = n / 60
    else:  # "min" or bare number
        minutes = n
    if minutes > TIME_MAX_MINUTES:  # implausible -> let it be treated as unknown
        return None
    return int(round(minutes))


def source_id_from_url(url):
    m = URL_ID.search(url or "")
    return m.group(1) if m else None


# Trailing product id in a yeast name, e.g. "...2565", "US-05", "WLP001", "S-04".
YEAST_PRODUCT_ID = re.compile(r"([A-Za-z]{0,4}-?\d[\w-]*)\s*$")


def parse_yeast(yeast_list):
    if not isinstance(yeast_list, list) or not yeast_list:
        return None
    name = (str(yeast_list[0]) if yeast_list[0] is not None else "").strip()
    # Junk placeholder rows like " - Default - - -".
    if not name or name.strip(" -").lower() in ("", "default"):
        return None

    yeast = RecipeYeast(kind_raw=clean_kind(name))
    if " - " in name:
        yeast.lab = name.split(" - ", 1)[0].strip() or None
    pid = YEAST_PRODUCT_ID.search(name)
    if pid:
        yeast.product_id = pid.group(1)[:32]

    if len(yeast_list) > 1:
        att = fnum(str(yeast_list[1]).replace("%", "")) if yeast_list[1] is not None else None
        att = bounded(att, 0, 120)
        if att is not None:
            yeast.min_attenuation = att
            yeast.max_attenuation = att
    return yeast


def build_result(rec):
    """Turn one Brewer's Friend JSON record into a ParserResult, or None to skip."""
    source_id = source_id_from_url(rec.get("url"))
    if not source_id:
        return None, None

    result = ParserResult()
    recipe = result.recipe
    recipe.name = (rec.get("name") or "").strip() or None
    recipe.style_raw = (rec.get("style") or "").strip() or None

    recipe.og = bounded(fnum(rec.get("og")), SG_MIN, SG_MAX)
    recipe.fg = bounded(fnum(rec.get("fg")), SG_MIN, SG_MAX)
    recipe.abv = bounded(fnum(rec.get("abv")), 0, ABV_MAX)
    recipe.ibu = bounded(fnum(rec.get("ibu")), 0, IBU_MAX)
    recipe.srm = bounded(fnum(rec.get("color")), 0, SRM_MAX)  # Color is SRM; save() -> ebc

    batch = fnum(rec.get("batch"))  # litres
    if batch is not None and batch > 0:
        recipe.cast_out_wort = int(round(batch))

    # Fermentables: [amount_kg, name, yield_pct, color_lovibond, amount_percent]
    for f in rec.get("fermentables") or []:
        if not isinstance(f, list) or len(f) < 2:
            continue
        name = clean_kind(f[1])
        if not name:
            continue
        amount_kg = fnum(f[0])
        result.fermentables.append(RecipeFermentable(
            kind_raw=name,
            amount=amount_kg * 1000 if amount_kg is not None else None,  # -> grams
            _yield=fnum(f[2]) if len(f) > 2 else None,
            color_lovibond=fnum(f[3]) if len(f) > 3 else None,
        ))

    # Hops: [amount_g, name, form, alpha, use, time, ibu_contrib, amount_percent]
    for h in rec.get("hops") or []:
        if not isinstance(h, list) or len(h) < 2:
            continue
        name = clean_kind(h[1])
        if not name:
            continue
        form = HOP_FORM.get(str(h[2]).lower()) if len(h) > 2 and h[2] else None
        result.hops.append(RecipeHop(
            kind_raw=name,
            amount=fnum(h[0]),  # grams
            form=form,
            alpha=fnum(h[3]) if len(h) > 3 else None,
            use=parse_hop_use(h[4]) if len(h) > 4 else RecipeHop.BOIL,
            time=parse_time_minutes(h[5]) if len(h) > 5 else None,
        ))

    yeast = parse_yeast(rec.get("yeast"))
    if yeast is not None:
        result.yeasts.append(yeast)

    return "%s:%s" % (SOURCE, source_id), result


def iter_records(path):
    """Stream the giant '{"0": {...}, "1": {...}}' file one record at a time.

    raw_decode() parses the first JSON object on the line and ignores the
    trailing ',' or final '}' -- so we never hold the whole 170MB in memory.
    """
    decoder = json.JSONDecoder()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line in ("{", "}"):
                continue
            if line[0] == "{":  # first line carries the outer brace
                line = line[1:].lstrip()
            brace = line.find("{")  # skip the "key": prefix
            if brace == -1:
                continue
            try:
                rec, _ = decoder.raw_decode(line[brace:])
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


class Command(BaseCommand):
    help = "Import the full Brewer's Friend recipes_full export (JSON-per-line with ingredients)."

    def add_arguments(self, parser):
        parser.add_argument("file_path", help="Path to recipes_full.txt")
        parser.add_argument(
            "--skip-existing", action="store_true",
            help="Skip ids already in the DB (default: replace them with this richer data)",
        )
        parser.add_argument("--limit", type=int, default=0, help="Stop after N imports (0 = all)")

    def handle(self, *args, **options):
        path = options["file_path"]
        skip_existing = options["skip_existing"]
        limit = options["limit"]
        loader = RecipeLoader()

        imported = skipped = bad = 0
        for i, rec in enumerate(iter_records(path)):
            if limit and imported >= limit:
                break

            uid, result = build_result(rec)
            if uid is None:
                bad += 1
                continue

            existing = Recipe.objects.filter(pk=uid)
            if existing.exists():
                if skip_existing:
                    skipped += 1
                    continue
                existing.delete()  # replace stub/older copy with the richer one

            try:
                loader.import_recipe(uid, result)
                imported += 1
            except Exception as e:
                bad += 1
                if bad <= 10:
                    self.stderr.write("record %d (%s): %s" % (i, uid, repr(e)[:160]))

            if imported and imported % 5000 == 0:
                self.stdout.write("  ... imported=%d skipped=%d bad=%d" % (imported, skipped, bad))

        self.stdout.write("Done. imported=%d skipped=%d bad=%d" % (imported, skipped, bad))
