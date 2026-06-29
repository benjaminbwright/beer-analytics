# Running beer-analytics locally (homelab lab → k3s graduation)

This fork adds a self-contained local stack that runs entirely in containers —
**nothing is installed on the host but the Docker engine**. The same image
graduates to k3s and can be pushed to the NAS registry.

## What's here (added on top of upstream)

| File | Purpose |
|------|---------|
| `docker-compose.local.yml` | App (gunicorn, **production image**) + MariaDB 11 + Elasticsearch 8. |
| `.env` | Local config + secret key. Gitignored — never committed. |
| WhiteNoise (in `pyproject.toml` + `settings_shared.py`) | Lets the app serve its own `/static/`, so one container needs no separate static web server — locally and in k8s alike. |

We use the **production** Dockerfile (`build/production/Dockerfile`), not the
upstream dev `docker-compose.yaml`. The dev rig bind-mounts source and needs
`yarn start` running on the host; the production multi-stage build compiles the
frontend (yarn) and Python deps (poetry) *inside* the image — the portable
artifact you actually deploy.

## First run

```bash
docker compose -f docker-compose.local.yml up -d --build      # build + start
docker compose -f docker-compose.local.yml exec django python manage.py migrate
docker compose -f docker-compose.local.yml exec django python manage.py load_initial_data
open http://localhost:8801
```

### Host ports (non-default, set in `.env`)

beer-analytics owns the **88xx** block so it never collides with other local
stacks. Container ports are unchanged; only the host side is remapped.

| Service | Host | Container |
|---------|------|-----------|
| app | `localhost:8801` | 5000 |
| MariaDB | `127.0.0.1:8836` | 3306 |
| Elasticsearch | `127.0.0.1:8892` | 9200 |

Give the next sandbox app its own block (e.g. 89xx) the same way.

Neither entrypoint runs `migrate`, so the first `migrate` above is required to
create the schema. `load_initial_data` seeds known styles and ingredients.

## Recipe data (not bundled — legal reasons)

The site is empty until you import recipes, then map and index them:

```bash
C="docker compose -f docker-compose.local.yml exec django python manage.py"
$C load_beerxml_recipe /app/var/raw_data/recipe.xml beerxml:my-recipe   # or load_mmum_recipe / load_beersmith_recipe
$C map_styles && $C map_hops && $C map_fermentables && $C map_yeasts
$C update_associated                  # REQUIRED: builds associated_* tables the charts read from
$C calculate_metrics && $C calculate_hop_pairings
$C refresh_elasticsearch_data        # populates the ES-backed search box
```

`update_associated` is easy to miss (the upstream README omits it) but is
**required** — the per-style/hop charts filter on the `associated_*` M2M tables,
not the direct FK, so without it every chart shows "Not enough data" even for
well-populated styles. On prod settings the chart JSON is cached (file cache), so
after a backfill clear it: `$C shell -c "from django.core.cache import caches;
[caches[a].clear() for a in ['default','data','images']]"`.

The `uid` **must** be `source:id` (exactly one colon, ≤ 32 chars) — e.g.
`beerxml:my-recipe`. A bare id crashes the loader (`source, source_id =
uid.split(":")`). Import is idempotent; re-add a uid with `--replace` to overwrite.

### MMUM (automated)

`fetch_mmum` pulls the maischemalzundmehr.de archive for you — see the bulk run
and the scheduled incremental below; no manual files needed.

### BeerXML / BeerSmith (manual files)

Both are one-recipe-per-file. Use a distinct source prefix per origin
(`beerxml:`, `beersmith:`, `bf:` …) so ids never collide.

Zero-setup smoke test using the bundled sample files (already in the image):

```bash
$C load_beerxml_recipe   /app/recipe_db/etl/format/fixtures/beerxml.xml   beerxml:coffee-stout
$C load_beersmith_recipe /app/recipe_db/etl/format/fixtures/beersmith.xml beersmith:sample
```

**Where to get files** — BeerXML: export from Brewer's Friend (public recipe →
Download → BeerXML), Brewfather, BeerSmith desktop, or brewtarget. BeerSmith:
beersmithrecipes.com (recipe → Download `.bsmx`) or the BeerSmith desktop app.

Batch-import a folder (BeerXML shown; swap the command + extension for `.bsmx`):

```bash
docker compose -f docker-compose.local.yml cp ./beerxml-files django:/app/var/raw_data/beerxml
docker compose -f docker-compose.local.yml exec django sh -c \
  'for f in /app/var/raw_data/beerxml/*.xml; do python manage.py load_beerxml_recipe "$f" "beerxml:$(basename "$f" .xml)"; done'
```

After any manual import, run the map/metrics/index block above so it shows in the
charts and search. If an export bundles many recipes in one file, split it first
(the loaders take a single uid per file).

## Scheduled incremental import

An [Ofelia](https://github.com/mcuadros/ofelia) `scheduler` service runs a daily
(04:00) incremental pull, defined in `ofelia.ini`. It execs into the app
container and runs:

```
fetch_mmum --since-max --stop-after-misses 50 --delay 2
  → map_* → calculate_metrics → calculate_hop_pairings → refresh_elasticsearch_data
```

`--since-max` starts just past the highest imported id and `--stop-after-misses`
halts once it hits 50 consecutive non-existent ids (the archive frontier), so a
run with nothing new costs ~50 requests — not a re-scan of the whole archive.
Jobs live in `ofelia.ini` (not as labels on the app), so editing the schedule
never forces a recreate of the running app container.

```bash
docker compose -f docker-compose.local.yml logs -f scheduler   # watch the cron
# run the incremental once, by hand:
docker compose -f docker-compose.local.yml exec django python manage.py fetch_mmum --since-max --stop-after-misses 50 --delay 2
```

Note: `--since-max` only walks forward from the frontier, so recipes *backfilled*
into old gaps won't be picked up; re-run a bounded `fetch_mmum --start A --end B`
occasionally if you want to sweep for those.

## Day-to-day

```bash
docker compose -f docker-compose.local.yml logs -f django   # tail app logs
docker compose -f docker-compose.local.yml ps               # status
docker compose -f docker-compose.local.yml down             # stop (keeps volumes/data)
docker compose -f docker-compose.local.yml down -v          # stop + wipe data
docker compose -f docker-compose.local.yml up -d --build     # rebuild after code changes
```

See the host-ports table above. MariaDB and ES are exposed localhost-only for
poking with a GUI; the app is on `0.0.0.0` so the Caddy proxy can reach it.

## Expose through the Caddy reverse proxy (optional, while still on the M5)

Add to `inventory/host_vars/proxy.yml` in the homelab repo and re-run the
`reverse_proxy` play (replace `<MAC-LAN-IP>` with the Mac's reserved LAN IP):

```yaml
- { name: beer, upstream: "10.0.0.31:8801" }   # internal-only; add public:true to expose
```

`10.0.0.31` is the M5's LAN IP — **reserve it** on the AT&T gateway so a DHCP
lease change doesn't break the upstream (it already drifted from .179 → .31).

Then it's reachable at `https://beer.h.casagreyhound.com` with the wildcard cert.

## Graduating to k3s

The URL stays the same; only Caddy's `upstream` changes (M5 → a NodePort on the
cluster). Steps when the cluster is ready:

1. **Multi-arch image.** This M5 builds `arm64`; `kube-srv-03` is `amd64`. Build
   for both so it runs anywhere in the estate:
   ```bash
   docker buildx build --platform linux/amd64,linux/arm64 \
     -f build/production/Dockerfile -t beer-analytics:1.0 --push .
   ```
2. **NAS registry.** Tag and push to the registry on `nas-01` (Traefik/servicelb
   are disabled in k3s, so this is a plain registry, not in-cluster):
   ```bash
   docker tag beer-analytics:1.0 registry.h.casagreyhound.com/beer-analytics:1.0
   docker push registry.h.casagreyhound.com/beer-analytics:1.0
   ```
3. **Manifests.** Deployment (the image above) + Service `type: NodePort` +
   MariaDB and Elasticsearch (StatefulSets or operators) + a PVC for DB data.
   Run `migrate`/`load_initial_data` as a Job or one-shot.
4. **Repoint Caddy** `upstream` to `<any-node-IP>:<nodePort>` and `kubectl apply`.
   Tear down the local compose once verified.
