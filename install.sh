#!/usr/bin/env bash
# Download only the launcher/Compose/Docker installer; Python stays in the image.
set -euo pipefail
umask 077
repository="${TG_GUARD_REPOSITORY:-TotKirich/tg-antidel-bot}"
[[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo 'Некорректный repository.' >&2; exit 1; }
if ! command -v curl >/dev/null 2>&1; then
  if (( EUID == 0 )); then apt-get update && apt-get install -y curl ca-certificates
  else sudo apt-get update && sudo apt-get install -y curl ca-certificates; fi
fi
directory="${TG_GUARD_INSTALL_DIR:-$HOME/tg-antidel-bot}"
if [[ -d "$directory" && ! -f "$directory/.tg-guard-install" ]] && [[ -n "$(find "$directory" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo 'Каталог не принадлежит установщику. Задайте новый TG_GUARD_INSTALL_DIR; файлы не перезаписаны.' >&2
  exit 1
fi
mkdir -p "$directory"
directory="$(cd "$directory" && pwd)"
stage="$(mktemp -d "$directory/.download.XXXXXX")"
cleanup() { rm -f -- "$stage/commit.json" "$stage/user.json" "$stage/start.sh" "$stage/compose.prebuilt.yaml" "$stage/Install-Docker.sh"; rmdir -- "$stage" 2>/dev/null || true; }
trap cleanup EXIT
fetch_api() {
  local endpoint="$1" output="$2" media="${3:-application/vnd.github+json}"
  if [[ -n "${GH_TOKEN:-}" ]]; then
    [[ "$GH_TOKEN" =~ ^[A-Za-z0-9_]+$ ]] || { echo 'Некорректный формат GitHub token.' >&2; return 1; }
    curl -q --fail --silent --show-error --location --proto '=https' --connect-timeout 15 --max-time 90 \
      --config <(printf 'header = "Authorization: Bearer %s"\n' "$GH_TOKEN") \
      -H "Accept: $media" "https://api.github.com/$endpoint" -o "$output"
  else
    curl -q --fail --silent --show-error --location --proto '=https' --connect-timeout 15 --max-time 90 \
      -H "Accept: $media" "https://api.github.com/$endpoint" -o "$output"
  fi
}
ready_endpoint="repos/$repository/actions/workflows/container.yml/runs?status=success&event=workflow_dispatch&per_page=1"
if ! fetch_api "$ready_endpoint" "$stage/commit.json"; then
  if [[ -z "${GH_TOKEN:-}" ]]; then
    read -r -s -p 'GitHub token для приватной V2 (repo и read:packages): ' GH_TOKEN </dev/tty
    printf '\n'
    export GH_TOKEN
    fetch_api "$ready_endpoint" "$stage/commit.json"
  else exit 1; fi
fi
source_ref="$(sed -nE 's/.*"head_sha": *"([0-9a-f]{40})".*/\1/p' "$stage/commit.json" | head -n 1)"
[[ "$source_ref" =~ ^[0-9a-f]{40}$ ]] || { echo 'Готовой сборки ещё нет. Запустите Actions → Build container manually.' >&2; exit 1; }
for file in start.sh compose.prebuilt.yaml scripts/Install-Docker.sh; do
  fetch_api "repos/$repository/contents/$file?ref=$source_ref" "$stage/${file##*/}" 'application/vnd.github.raw+json'
done
mkdir -p "$directory/scripts"
install -m 0755 "$stage/start.sh" "$directory/start.sh"
install -m 0644 "$stage/compose.prebuilt.yaml" "$directory/compose.prebuilt.yaml"
install -m 0755 "$stage/Install-Docker.sh" "$directory/scripts/Install-Docker.sh"
touch "$directory/.tg-guard-install"
printf '%s\n' "$source_ref" > "$directory/.source-ref"
export TG_GUARD_IMAGE="ghcr.io/${repository,,}:sha-$source_ref"
if [[ -n "${GH_TOKEN:-}" ]]; then
  fetch_api user "$stage/user.json"
  export TG_GUARD_REGISTRY_USER="$(sed -nE 's/.*"login": *"([A-Za-z0-9_-]+)".*/\1/p' "$stage/user.json" | head -n 1)"
  export TG_GUARD_REGISTRY_TOKEN="$GH_TOKEN"
fi
echo 'Исходники приложения не скачиваются. Запускаю готовый контейнерный образ.'
bash "$directory/start.sh" "$@"
