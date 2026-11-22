#!/usr/bin/env bash
# Apply the tested submission release to the existing SoftX systemd service.
set -euo pipefail
umask 022
test "$EUID" -eq 0 || { echo 'Run from the VPS console as root.' >&2; exit 1; }

backend_commit=c34ba3fc7d381663701c2a5c1c0fe29da3b1fb5a
client_commit=768832be803cb6317fd41ee6d2e0c00690e4eb81
service=softx.service
work=$(systemctl show -p WorkingDirectory --value "$service")
pid=$(systemctl show -p MainPID --value "$service")
test -n "$work" && test "$pid" -gt 0 && test -d "$work/softx"
python="$work/.venv/bin/python"
test -x "$python" || { echo "Existing Python environment unavailable: $python" >&2; exit 1; }
work=$(realpath "$work")
static=$("$python" - "$pid" "$work" <<'PY'
from pathlib import Path
import sys
pid, working_directory = sys.argv[1:]
for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
    if item.startswith(b"SOFTX_STATIC_DIR="):
        path = Path(item.split(b"=", 1)[1].decode())
        print((Path(working_directory) / path).resolve())
        break
else:
    print(Path(working_directory) / "softx" / "static")
PY
)
test -d "$static"
asset_names=(app.js index.html style.css softx-logo.svg photovoltaic-banner.webp)
for asset in "${asset_names[@]}"; do test -f "$static/$asset"; done

stage=$(mktemp -d "$work/.softx-submission-stage.XXXXXX")
backup=$(mktemp -d "$work/.softx-before-submission.XXXXXX")
backup_external=0
moved_code=0
stopped=0
success=0

cleanup() {
    local status=$?
    trap - EXIT
    if (( ! success && stopped )); then
        echo 'Release check failed. Restoring the previous application files.' >&2
        systemctl stop "$service" || true
        if (( moved_code )); then
            if test -d "$work/softx"; then mv "$work/softx" "$stage/rejected-softx"; fi
            mv "$backup/softx" "$work/softx"
        fi
        if (( backup_external )); then
            for asset in "${asset_names[@]}"; do cp -p "$backup/static/$asset" "$static/$asset"; done
        fi
        systemctl start "$service"
        echo "Rollback checked with systemctl: $(systemctl is-active "$service")" >&2
    fi
    rm -rf -- "$stage"
    exit "$status"
}
trap cleanup EXIT

mkdir "$stage/server" "$stage/client" "$stage/test-data"
curl -fsSL --retry 2 -o "$stage/server.tar.gz" "https://codeload.github.com/SaSitme/softx-science/tar.gz/$backend_commit"
curl -fsSL --retry 2 -o "$stage/client.tar.gz" "https://codeload.github.com/SaSitme/softx-science-client/tar.gz/$client_commit"
tar --extract --gzip --file="$stage/server.tar.gz" --directory="$stage/server" --strip-components=1 --no-same-owner
tar --extract --gzip --file="$stage/client.tar.gz" --directory="$stage/client" --strip-components=1 --no-same-owner

(
    cd "$stage/server"
    SOFTX_DATA="$stage/test-data" SOFTX_STATIC_DIR="$stage/client" SOFTX_DEMO=0 SOFTX_LOCAL_ONLY=1 "$python" - <<'PY'
from io import BytesIO
from zipfile import ZipFile
from softx.app import app
with app.test_client() as client:
    assert b"app.js?v=submission" in client.get("/").data
    assert b"submission.zip" in client.get("/static/app.js").data
    response = client.get("/api/export/submission.zip")
    assert response.status_code == 200
    with ZipFile(BytesIO(response.data)) as archive:
        assert sorted(archive.namelist()) == ["devices.csv", "issues.csv", "layers.csv", "materials.csv",
                                               "measurements.csv", "publications.csv", "results.csv"]
PY
)

if [[ "$static" == "$work/softx/static" ]]; then
    mkdir "$stage/server/softx/static"
    for asset in "${asset_names[@]}"; do
        install -m 644 "$stage/client/$asset" "$stage/server/softx/static/$asset"
    done
else
    mkdir "$backup/static"
    for asset in "${asset_names[@]}"; do cp -p "$static/$asset" "$backup/static/$asset"; done
    backup_external=1
fi

systemctl stop "$service"
stopped=1
mv "$work/softx" "$backup/softx"
moved_code=1
mv "$stage/server/softx" "$work/softx"
if (( backup_external )); then
    for asset in "${asset_names[@]}"; do
        install -m 644 "$stage/client/$asset" "$static/$asset"
    done
fi
systemctl start "$service"
for _ in {1..20}; do
    if curl -fsS --max-time 4 https://s0ftx.ru/ -o "$stage/home.html" &&
       grep -Fq 'app.js?v=submission' "$stage/home.html" &&
       curl -fsS --max-time 4 'https://s0ftx.ru/static/app.js?v=submission' -o "$stage/site-app.js" &&
       cmp -s "$stage/client/app.js" "$stage/site-app.js" &&
       curl -fsS --max-time 4 https://s0ftx.ru/api/export/submission.zip -o "$stage/live.zip"; then
        "$python" - "$stage/live.zip" <<'PY'
from pathlib import Path
import sys
from zipfile import ZipFile
with ZipFile(Path(sys.argv[1])) as archive:
    assert archive.testzip() is None and len(archive.namelist()) == 7
PY
        success=1
        echo "SOFTX RELEASE OK. Previous application saved at $backup"
        exit 0
    fi
    sleep 1
done
exit 1
