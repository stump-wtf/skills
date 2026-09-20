# Request front-end paths — the commands

Companion to the "The request front-end submits a path" section of `SKILL.md`.
Read it when a Jellyseerr request fails and you need the exact calls.

## Ask each *arr what its root folders really are

The API key is in each app's `config.xml`. Read it into a variable; never print it.

```bash
# on the DERIVED media host, as joestump
for pair in sonarr:8989 radarr:7878 lidarr:8686; do
  app="${pair%%:*}"; port="${pair##*:}"
  K=$(sudo grep -oP '(?<=<ApiKey>)[^<]+' "/volumes/$app/config/config.xml") || continue
  echo "== $app"
  curl -s -H "X-Api-Key: $K" "http://localhost:$port/api/v3/rootfolder" \
    | python3 -c 'import sys,json;[print(" ", r["path"], "accessible=", r["accessible"]) for r in json.load(sys.stdin)]'
done
```

`accessible: false` means the bind mount moved under the service. That is an
inventory change — file an issue, do not repoint it by hand.

Note Lidarr is `/api/v1`, not `/api/v3` — see `api-notes.md` for the version split.

## Ask Jellyseerr what it will submit

```bash
ssh ie01.stump.rocks 'sudo python3 -c "
import json
d = json.load(open(\"/volumes/jellyseerr/config/settings.json\"))
for kind in (\"radarr\", \"sonarr\"):
    for s in d.get(kind, []):
        print(kind, s[\"name\"], s[\"activeDirectory\"])
"'
```

The two lists must agree. A mismatch is the front-end's own stale state — it is
not Ansible-managed, so no converge will fix it and none broke it.

## Correct it

Back up first. Patch only `activeDirectory`; everything else in that file is
live configuration you did not come to change.

```bash
ssh ie01.stump.rocks 'sudo sh -c "
F=/volumes/jellyseerr/config/settings.json
cp -p \$F \$F.bak-\$(date +%Y%m%d-%H%M%S)
"'
# then a python patch setting radarr -> /data/Movies, sonarr -> /data/TV
# (derive those values from the rootfolder call above — do not paste them from here)
ssh ie01.stump.rocks 'sudo docker restart jellyseerr'
```

## Verify by retrying, not by re-reading

Re-reading the setting proves the write landed. Only a retry proves the path
works end to end.

```bash
# inside the host; IP because jellyseerr publishes no port (caddy-docker-proxy)
K=$(sudo python3 -c 'import json;print(json.load(open("/volumes/jellyseerr/config/settings.json"))["main"]["apiKey"])')
IP=$(sudo docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' jellyseerr)
B="http://$IP:5055/api/v1"

curl -s -H "X-Api-Key: $K" "$B/request?filter=failed&take=20" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("failed:",d["pageInfo"]["results"]);[print(" ",r["id"],r["type"]) for r in d["results"]]'

curl -s -o /dev/null -w '%{http_code}\n' -X POST -H "X-Api-Key: $K" "$B/request/<id>/retry"
```

Then the two checks that actually settle it:

```bash
sudo docker logs --since 5m jellyseerr 2>&1 | grep -c RootFolderExistsValidator   # want 0
curl -s -H "X-Api-Key: $K" "$B/request?filter=failed&take=1" \
  | python3 -c 'import sys,json;print("failed now:",json.load(sys.stdin)["pageInfo"]["results"])'
```

A retry re-drives real acquisitions and fires searches at your indexers, so
report how many you retried rather than doing it quietly.

## Reporting

Say which host you derived, the two path lists before and after, how many
requests you retried, and the failed count now. "Fixed" without the failed
count is not a verified claim.
