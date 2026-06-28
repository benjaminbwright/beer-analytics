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
$C load_beerxml_recipe /app/var/raw_data/recipe.xml my-unique-id   # or load_mmum_recipe / load_beersmith_recipe
$C map_styles && $C map_hops && $C map_fermentables && $C map_yeasts
$C calculate_metrics && $C calculate_hop_pairings
$C refresh_elasticsearch_data        # populates the ES-backed search box
```

Drop import files into the `app_var` volume at `/app/var/raw_data` (host copy:
`docker compose -f docker-compose.local.yml cp ./recipe.xml django:/app/var/raw_data/`).

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
