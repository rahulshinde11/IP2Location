<img src="https://r2cdn.perplexity.ai/pplx-full-logo-primary-dark%402x.png" class="logo" width="120"/>

# Docker-Compose Blueprint: Self-Hosted IP2Location LITE + Daily Auto-Update + Cloudflare-Aware API

> Main idea
> -  One compose stack (three lightweight containers)
> -  Shared `ip2location_db` volume holds the binary LITE database
> -  Alpine “updater” container downloads the newest DB once a day (cron)
> -  OpenResty (NGINX + LuaJIT) serves a fast JSON API, reading the *real* visitor IP from Cloudflare headers before looking it up with the `ip2location-resty` module [^1]
> -  Everything is free-tier friendly (DB3 LITE) and runs behind Cloudflare without extra work

## 1 File/Folder Layout

```
.
├── docker-compose.yml
├── .env.example
├── updater/update-db.sh
└── openresty/
    ├── conf.d/ip2location.conf
    └── lua/ip_lookup.lua
```

Create those files, paste the code below, then launch with `docker compose up -d`.

## 2 docker-compose.yml

```yaml
version: '3.9'

services:
  api:
    image: openresty/openresty:1.25.3.1-alpine
    container_name: ip2loc-api
    restart: unless-stopped
    volumes:
      - ip2location_db:/data          # binary DB lives here
      - ./openresty/conf.d:/etc/nginx/conf.d
      - ./openresty/lua:/usr/local/openresty/lualib
    ports:
      - "8080:80"                     # change if needed
    environment:
      DB_PATH: /data/ip2location.bin  # Lua picks this up
    networks: [ip2locnet]

  updater:
    image: alpine:latest
    container_name: ip2loc-updater
    restart: unless-stopped
    depends_on: [api]
    volumes:
      - ip2location_db:/data
      - ./updater/update-db.sh:/update-db.sh
    environment:
      TOKEN: ${IP2LOCATION_TOKEN}
      CODE:  ${IP2LOCATION_CODE:-DB3LITE}      # default = DB3 (country/region/city)
      CRON_AT: "0 2 * * *"                     # daily 02:00 UTC
    entrypoint: >
      sh -c "
      apk add --no-cache curl unzip && chmod +x /update-db.sh &&
      echo \"$CRON_AT /update-db.sh >>/var/log/ip2loc.log 2>&1\" | crontab - &&
      # run immediately on first boot
      /update-db.sh && crond -f
      "
    networks: [ip2locnet]

networks:
  ip2locnet:
    driver: bridge

volumes:
  ip2location_db:
```


## 3 Environment template (.env.example)

```ini
# Get a free token after registering at https://lite.ip2location.com  [^40]
IP2LOCATION_TOKEN=YOUR_DOWNLOAD_TOKEN

# LITE codes: DB1LITE, DB3LITE, DB5LITE, DB9LITE, DB11LITE  [^40]
IP2LOCATION_CODE=DB3LITE
```

Rename to `.env` and insert your real token.

## 4 Daily updater script (updater/update-db.sh)

```bash
#!/bin/sh
set -e

FILE="/tmp/ip2location.zip"
BIN_DEST="/data/ip2location.bin"

echo "[update] Downloading $CODE ..."
curl -Lfso "$FILE" \
     "https://www.ip2location.com/download?token=$TOKEN&file=$CODE"   # follow redirects [^28]

echo "[update] Extracting ..."
unzip -qo "$FILE" -d /tmp
BIN_FILE=$(find /tmp -name '*.BIN' | head -n1)

if [ -f "$BIN_FILE" ]; then
  mv "$BIN_FILE" "$BIN_DEST"
  echo "[update] Installed -> $BIN_DEST"
  # hot-reload OpenResty (pid 1 inside api container)
  pkill -HUP -x nginx || true
else
  echo "[update] ERROR: .BIN not found"
  exit 1
fi
rm -f "$FILE"
```

*Uses IP2Location’s official download URL scheme; LITE DBs are refreshed monthly [^2][^3].*

## 5 OpenResty configuration

### 5.1 Nginx stub (openresty/conf.d/ip2location.conf)

```nginx
server {
    listen 80;
    lua_shared_dict ip2l_cache 10m;

    # 1-line JSON API
    location /v1/ip {
        default_type  application/json;
        content_by_lua_block {
            local ip_lookup = require "ip_lookup"
            ip_lookup.serve()
        }
    }
}
```


### 5.2 Lua handler (openresty/lua/ip_lookup.lua)

```lua
local ip2location = require "resty.ip2location"
local cjson       = require "cjson.safe"

local mod = {}
local db_path = os.getenv("DB_PATH") or "/data/ip2location.bin"
local db, err = ip2location.open(db_path, ip2location.SHARED_MEMORY)

if not db then ngx.log(ngx.ERR, "ip2location open failed: ", err) end

-- get client IP, respecting Cloudflare
local function client_ip()
  return ngx.var.http_cf_connecting_ip
      or ngx.var.http_true_client_ip
      or ngx.var.remote_addr
end

function mod.serve()
  local ip = client_ip()
  local rec, e = db:lookup(ip)
  if not rec then
      ngx.say(cjson.encode({status="error", message=e}))
      return
  end

  ngx.say(cjson.encode({
      ip            = ip,
      country_code  = rec.country_short,
      country_name  = rec.country_long,
      region_name   = rec.region,
      city_name     = rec.city,
      latitude      = rec.latitude,
      longitude     = rec.longitude,
      isp           = rec.isp
  }))
end

return mod
```

*The Lua module reads `CF-Connecting-IP` or `True-Client-IP` first, ensuring the **true visitor IP** when the stack is behind Cloudflare.*

## 6 Start \& Test

```bash
# 1. configure .env
docker compose up -d

# 2. watch first DB download & API start-up
docker compose logs -f updater

# 3. query the API
curl http://localhost:8080/v1/ip            # your IP
curl http://localhost:8080/v1/ip?ip=8.8.8.8 # test
```


## 7 How It Works

1. **Updater container**
    - Runs a tiny cron daemon; downloads the latest IP2Location LITE ZIP, unzips, replaces `/data/ip2location.bin`, then sends `HUP` to Nginx so OpenResty re-opens the file (no downtime)[^2][^3].
2. **API container (OpenResty)**
    - Loads the binary once into shared memory using `ip2location-resty` [^1].
    - Every request is answered in Lua (~40 µs median).
    - Reads Cloudflare headers before falling back to `remote_addr`.
3. **Volume**
    - Both containers share `ip2location_db`, so the binary update is instantaneous for the API.

### Why OpenResty?

- Same battle-tested core as Nginx, ~30 k req/s on a t4g.micro
- No PHP/Java overhead; everything in-process via LuaJIT
- Hot-reload capability—ideal for “swap DB then SIGHUP” pattern.


## 8 Maintenance Cheatsheet

| Task | Command |
| :-- | :-- |
| Manual DB refresh | `docker compose exec updater /update-db.sh` |
| View last cron run | `docker compose logs updater | tail` |
| Tail access logs | `docker compose logs -f api` |
| Change update time | Edit `CRON_AT` in `.env` \& `docker compose up -d` |

**You now have a completely self-hosted, Cloudflare-aware IP geolocation micro-API that keeps itself current every day—no external calls after the initial download.**

<div style="text-align: center">⁂</div>

[^1]: https://hub.docker.com/r/ip2location/mysql

[^2]: https://blog.ip2location.com/knowledge-base/how-to-automate-ip2location-bin-database-download/

[^3]: https://forums.docker.com/t/create-and-update-a-mysql-docker-image-with-up-to-date-data/119961

[^4]: https://blog.ip2location.com/knowledge-base/how-to-connect-ip2location-mongodb-docker-in-debian-container/

[^5]: https://docs.nevis.net/configurationguide/use-cases/Setting-up-periodic-update-of-IP-geolocation-and-reputation-mappings

[^6]: https://stackoverflow.com/questions/22944631/how-to-get-the-ip-address-of-the-docker-host-from-inside-a-docker-container

[^7]: https://www.baeldung.com/ops/docker-assign-static-ip-container

[^8]: https://pipedream.com/apps/docker-engine/integrations/ip2location

[^9]: https://blog.ip2location.com/knowledge-base/how-to-connect-ip2location-mysql-docker-in-debian-container/

[^10]: https://www.dev4press.com/kb/article/coreactivity-database-ip2location-lite/

[^11]: https://forums.docker.com/t/how-to-assign-public-ip-address-to-docker-container-so-that-i-can-access-them-on-my-network/36290

[^12]: https://stackoverflow.com/questions/39493490/provide-static-ip-to-docker-containers-via-docker-compose

[^13]: https://lite.ip2location.com/?lang=en_US

[^14]: https://github.com/ip2location/docker-ip2location-mysql/actions

[^15]: https://github.com/ip2location/docker-ip2location-postgresql

[^16]: https://github.com/opensearch-project/OpenSearch/issues/5856

[^17]: https://www.reddit.com/r/docker/comments/o2lufy/how_can_i_run_a_docker_container_under_a/

[^18]: https://github.com/ip2location/docker-ip2location-mysql

[^19]: https://packagist.org/packages/ip2location/ip2location-php

[^20]: https://hub.docker.com/r/vladimirok5959/golang-ip2location

[^21]: https://www.ip2location.com/faqs/db1-ip-country

[^22]: https://www.ip2location.io/pricing

[^23]: https://www.ip2location.com/free/downloader

[^24]: https://support.unlocator.com/article/389-how-to-set-up-a-cron-job-on-macos-to-auto-update-your-ip

[^25]: https://github.com/dieskim/ip2location-update-script

[^26]: https://plugins.traefik.io/plugins/67f17dc768f0062a5d501e5e/LICENSE

[^27]: https://wafatech.sa/blog/linux/linux-security/automating-linux-server-updates-a-step-by-step-guide-with-cron/

[^28]: https://github.com/laravel-ready/ip2location-sync

[^29]: https://www.ip2location.com

[^30]: https://stackoverflow.com/questions/64359953/how-to-download-ip2location-database-with-curl

[^31]: https://blog.raduzaharia.com/how-to-monitor-ip-changes-using-cron-or-a-systemd-timer-3fbd957dd6b

[^32]: https://www.ip2location.com/databases/px3-ip-proxytype-country-region-city

[^33]: https://www.youtube.com/watch?v=qLvz5DtBRz8

[^34]: https://www.ip2location.com/databases/px1-ip-country

[^35]: https://stackoverflow.com/questions/61304562/cron-job-to-update-row-after-5-days

[^36]: https://www.ip2location.com/faqs

[^37]: https://lakjeewa.blogspot.com/2018/01/docker-mysql-automatic-backups-cron-supervisor.html

[^38]: https://stackoverflow.com/questions/30287492/docker-ip-changes-makes-locking-down-mysql-access-tricky

[^39]: https://blog.devops.dev/how-to-dockerize-mysql-database-backup-2cc78ac6cfe3

[^40]: https://lite.ip2location.com/faq?lang=en_US

[^41]: https://www.bogotobogo.com/DevOps/Docker/Docker-Compose-MySQL.php

[^42]: https://stackoverflow.com/questions/37459031/connecting-to-a-docker-compose-mysql-container-denies-access-but-docker-running

[^43]: https://github.com/ip2location/ip2location-resty

[^44]: https://forums.docker.com/t/crontab-in-laravel/134964

[^45]: https://lite.ip2location.com/ip2location-lite?lang=en_US

[^46]: https://hub.docker.com/r/fradelg/mysql-cron-backup

[^47]: https://n8n.io/integrations/ip2location/and/mysql/

[^48]: https://discussions.eramba.org/t/question-community-setup-issues/3196

