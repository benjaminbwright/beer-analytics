import os
import time
from json import JSONDecodeError

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from recipe_db.etl.format.mmum import MmumParser
from recipe_db.etl.loader import RecipeFileProcessor, RecipeLoader
from recipe_db.models import Recipe

# MMUM (maischemalzundmehr.de) publishes one JSON document per recipe at this
# endpoint, keyed by a sequential integer id. The body is exactly what
# MmumParser expects (German keys: Name, Autor, Sorte, Stammwuerze, ...).
EXPORT_URL = "https://www.maischemalzundmehr.de/export_json.php?id={id}"
SOURCE = "mmum"
# Honest, identifying UA — this is a personal homelab pull, not anonymous traffic.
USER_AGENT = "beer-analytics-homelab/1.0 (personal homelab analytics)"


class Command(BaseCommand):
    help = "Fetch recipes from maischemalzundmehr.de (MMUM JSON export) and import them. Polite + resumable."

    def add_arguments(self, parser):
        parser.add_argument("--start", type=int, default=1, help="First recipe id (inclusive)")
        parser.add_argument("--end", type=int, default=2300, help="Last recipe id (inclusive)")
        parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests (be polite)")
        parser.add_argument("--timeout", type=float, default=20.0, help="HTTP timeout per request (s)")
        parser.add_argument("--replace", action="store_true", help="Re-download + re-import ids already in the DB")
        parser.add_argument("--max-failures", type=int, default=0,
                            help="Stop after this many CONSECUTIVE errors (0 = never stop)")
        parser.add_argument("--since-max", action="store_true",
                            help="Start just past the highest already-imported mmum id (frontier-only incremental)")
        parser.add_argument("--stop-after-misses", type=int, default=0,
                            help="Stop after this many CONSECUTIVE non-existent ids (the archive frontier; 0 = off)")

    def handle(self, *args, **options):
        start, end = options["start"], options["end"]
        delay, timeout = options["delay"], options["timeout"]
        replace = options["replace"]
        max_consec = options["max_failures"]
        stop_after_misses = options["stop_after_misses"]

        # Frontier-only incremental: begin just past the highest imported id so we
        # only request genuinely new recipes, not the whole archive's gaps.
        if options["since_max"]:
            existing = Recipe.objects.filter(source=SOURCE).values_list("source_id", flat=True)
            nums = [int(s) for s in existing if str(s).isdigit()]
            start = (max(nums) + 1) if nums else 1
            self.stdout.write("since-max: starting at id %d" % start)

        out_dir = os.path.join(settings.__getattr__("RAW_DATA_DIR"), SOURCE)
        os.makedirs(out_dir, exist_ok=True)

        processor = RecipeFileProcessor(RecipeLoader(), [MmumParser()], replace_existing=replace)
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})

        imported = skipped = missing = failed = 0
        consec = 0          # consecutive request/import errors
        consec_missing = 0  # consecutive non-existent ids (frontier detector)

        self.stdout.write("Fetching MMUM ids %d-%d (delay=%ss)" % (start, end, delay))
        for rid in range(start, end + 1):
            uid = "%s:%d" % (SOURCE, rid)

            # Resume cheaply: don't re-request what's already imported.
            if not replace and Recipe.objects.filter(pk=uid).exists():
                skipped += 1
                consec_missing = 0
                continue

            try:
                resp = session.get(EXPORT_URL.format(id=rid), timeout=timeout)
            except requests.RequestException as e:
                failed += 1
                consec += 1
                self.stderr.write("id %d: request error: %s" % (rid, e))
                if max_consec and consec >= max_consec:
                    self.stderr.write("Stopping: %d consecutive failures" % consec)
                    break
                time.sleep(delay)
                continue

            # Non-existent ids return HTTP 200 with an HTML wrapper, not JSON.
            text = resp.text.strip()
            try:
                data = resp.json()
                valid = isinstance(data, dict) and bool(data.get("Name"))
            except (JSONDecodeError, ValueError):
                valid = False

            if not valid:
                missing += 1
                consec = 0
                consec_missing += 1
                if stop_after_misses and consec_missing >= stop_after_misses:
                    self.stdout.write("Reached frontier: %d consecutive misses ending at id %d"
                                      % (consec_missing, rid))
                    break
                time.sleep(delay)
                continue

            # Keep the raw JSON for provenance, then import from that file.
            file_path = os.path.join(out_dir, "%d.json" % rid)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(text)

            try:
                _, created = processor.import_recipe_from_file([file_path], uid)
                imported += 1 if created else 0
                skipped += 0 if created else 1
                consec = 0
                consec_missing = 0
            except Exception as e:
                failed += 1
                consec += 1
                self.stderr.write("id %d: import error: %s" % (rid, e))
                if max_consec and consec >= max_consec:
                    self.stderr.write("Stopping: %d consecutive failures" % consec)
                    break

            if rid % 50 == 0:
                self.stdout.write("  ... id %d | imported=%d skipped=%d missing=%d failed=%d"
                                  % (rid, imported, skipped, missing, failed))
            time.sleep(delay)

        self.stdout.write("Done. imported=%d skipped=%d missing=%d failed=%d (ids %d-%d)"
                          % (imported, skipped, missing, failed, start, end))
