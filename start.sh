#!/usr/bin/env bash
set -euo pipefail
umask 077
BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"
reconfigure=()
for option in "$@"; do
  case "$option" in
    --docker) ;; # Compatibility: V2 uses Docker only.
    --setup) reconfigure=(--reconfigure) ;;
    --help)
      echo 'Использование: bash start.sh [--setup]'
      echo 'Docker/Compose устанавливаются автоматически, если Docker отсутствует (Ubuntu/Debian, нужен sudo).'
      echo 'Первый запуск: мастер API Telegram → вход → бот уведомлений.'
      echo '--setup: повторная настройка, ключи скрыты, данные сохраняются.'
      echo 'TG_GUARD_AUTH_HOST / TG_GUARD_AUTH_PORT: адрес и порт новой установки.'
      exit 0 ;;
    *) echo "Неизвестный параметр: $option" >&2; exit 2 ;;
  esac
done

export TG_GUARD_DATA_DIR="${TG_GUARD_DATA_DIR:-$BASE_DIR/data}"
mkdir -p "$TG_GUARD_DATA_DIR"
if [[ -t 0 && -f "$TG_GUARD_DATA_DIR/config.json" && ${#reconfigure[@]} == 0 ]]; then
  echo '1 — Запустить с сохранёнными настройками (ключи скрыты)'
  echo '2 — Открыть мастер перенастройки (база и архив сохранятся)'
  read -r -p 'Выбор [1]: ' choice
  case "${choice:-1}" in
    1) ;;
    2) reconfigure=(--reconfigure) ;;
    *) echo 'Отменено.'; exit 0 ;;
  esac
fi

prepare_options=(prepare "${reconfigure[@]}")
if [[ ! -f "$TG_GUARD_DATA_DIR/config.json" ]]; then
  # Route lookup chooses a local interface without sending a network request.
  detected_host="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++)if($i=="src"){print $(i+1);exit}}' || true)"
  case "$detected_host" in
    10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) ;;
    *) detected_host=127.0.0.1 ;;
  esac
  prepare_options+=(--host "${TG_GUARD_AUTH_HOST:-$detected_host}" --port "${TG_GUARD_AUTH_PORT:-8080}")
fi
if ! command -v docker >/dev/null 2>&1; then
  bash scripts/Install-Docker.sh
fi
export TG_GUARD_UID="$(id -u)" TG_GUARD_GID="$(id -g)"
if (( EUID == 0 )); then
  export TG_GUARD_UID="${SUDO_UID:-1000}" TG_GUARD_GID="${SUDO_GID:-1000}"
  if [[ ! -f "$TG_GUARD_DATA_DIR/config.json" ]]; then
    chown "$TG_GUARD_UID:$TG_GUARD_GID" "$TG_GUARD_DATA_DIR"
  fi
fi
use_sudo=false
if ! docker info >/dev/null 2>&1; then
  if (( EUID != 0 )) && command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    use_sudo=true
  else
    echo 'Docker установлен, но daemon недоступен. Проверьте systemctl status docker и права доступа.' >&2
    exit 1
  fi
fi
docker_cmd() {
  if [[ "$use_sudo" == true ]]; then
    sudo --preserve-env=TG_GUARD_DATA_DIR,TG_GUARD_UID,TG_GUARD_GID,TG_GUARD_IMAGE,DOCKER_CONFIG docker "$@"
  else
    docker "$@"
  fi
}
if ! docker_cmd compose version >/dev/null 2>&1; then
  echo 'В существующей установке Docker отсутствует Compose plugin; установите docker-compose-plugin. Существующий Engine не заменялся.' >&2
  exit 1
fi
if [[ -z "${TG_GUARD_IMAGE:-}" && -f .image-ref ]]; then export TG_GUARD_IMAGE="$(cat .image-ref)"; fi
compose_file=compose.yaml
if [[ -n "${TG_GUARD_IMAGE:-}" ]]; then compose_file=compose.prebuilt.yaml; fi
compose=(docker_cmd compose --project-directory "$BASE_DIR" -p "${TG_GUARD_PROJECT:-tg-antidel}" -f "$compose_file")
if [[ -n "${TG_GUARD_COMPOSE_OVERRIDE:-}" ]]; then compose+=(-f "$TG_GUARD_COMPOSE_OVERRIDE"); fi
auth_directory=
cleanup_registry() {
  if [[ -n "$auth_directory" ]]; then
    rm -f -- "$auth_directory/config.json"
    rmdir -- "$auth_directory" 2>/dev/null || true
    unset DOCKER_CONFIG TG_GUARD_REGISTRY_TOKEN GH_TOKEN
    auth_directory=
  fi
}
trap cleanup_registry EXIT
if [[ -n "${TG_GUARD_IMAGE:-}" ]]; then
  if ! docker_cmd image inspect "$TG_GUARD_IMAGE" >/dev/null 2>&1; then
    if [[ -n "${TG_GUARD_REGISTRY_TOKEN:-}" ]]; then
      auth_directory="$(mktemp -d /dev/shm/tg-guard-auth.XXXXXX)"
      export DOCKER_CONFIG="$auth_directory"
      printf '%s' "$TG_GUARD_REGISTRY_TOKEN" | docker_cmd login ghcr.io -u "${TG_GUARD_REGISTRY_USER:?Registry username required}" --password-stdin
    fi
    "${compose[@]}" pull
    cleanup_registry
  fi
  digest="$(docker_cmd image inspect --format '{{index .RepoDigests 0}}' "$TG_GUARD_IMAGE")"
  if [[ "$digest" == ghcr.io/*@sha256:* ]]; then export TG_GUARD_IMAGE="$digest"; fi
  printf '%s\n' "$TG_GUARD_IMAGE" > .image-ref
else
  "${compose[@]}" build --quiet
fi
if (( ${#reconfigure[@]} )); then "${compose[@]}" stop userbot; fi
"${compose[@]}" run --rm --no-deps -T userbot python onboarding.py "${prepare_options[@]}"
"${compose[@]}" up -d --pull never
"${compose[@]}" run --rm --no-deps -T userbot python onboarding.py link
echo 'Контейнеры работают в фоне; Docker включённый в автозапуск поднимет их после перезагрузки.'
