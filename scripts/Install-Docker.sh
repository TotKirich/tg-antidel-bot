#!/usr/bin/env bash
# Docker's official apt repository; never remove existing container runtimes.
set -euo pipefail

detect_docker_os() {
  local release_file="${1:-/etc/os-release}"
  local ID= VERSION_CODENAME= UBUNTU_CODENAME=
  [[ -r "$release_file" ]] || { echo 'Не найден /etc/os-release.' >&2; return 1; }
  # /etc/os-release is a trusted local OS file, not downloaded shell code.
  source "$release_file"
  DOCKER_DISTRO="$ID"
  DOCKER_SUITE="${UBUNTU_CODENAME:-$VERSION_CODENAME}"
  case "$DOCKER_DISTRO:$DOCKER_SUITE" in
    ubuntu:jammy|ubuntu:noble|ubuntu:resolute|ubuntu:stonking|debian:bookworm|debian:trixie) ;;
    *) echo 'Автоустановка поддерживает Ubuntu 22.04/24.04/26.04/26.10 и Debian 12/13. Для этой ОС установите Docker по docs.docker.com.' >&2; return 1 ;;
  esac
}

check_docker_conflicts() {
  local package status
  for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
    status="$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)"
    if [[ "$status" == installed ]]; then
      echo "Обнаружен пакет $package. Автоматическая замена существующего container runtime запрещена; разрешите конфликт вручную." >&2
      return 1
    fi
  done
  if [[ -e /etc/apt/sources.list.d/docker.sources || -e /etc/apt/sources.list.d/docker.list ]]; then
    echo 'Репозиторий Docker уже настроен, но команда docker не найдена. Проверьте существующую установку вручную.' >&2
    return 1
  fi
}

install_docker() {
  if command -v docker >/dev/null 2>&1; then return 0; fi
  detect_docker_os
  command -v systemctl >/dev/null 2>&1 || { echo 'Для автоустановки нужен systemd.' >&2; return 1; }
  if (( EUID != 0 )); then
    command -v sudo >/dev/null 2>&1 || { echo 'Для установки Docker нужны root или sudo.' >&2; return 1; }
    exec sudo -- bash "${BASH_SOURCE[0]}"
  fi
  check_docker_conflicts
  echo 'Устанавливаю Docker Engine и Compose из официального apt-репозитория Docker.'
  apt-get update
  apt-get install -y ca-certificates curl
  local temporary_key
  temporary_key="$(mktemp)"
  trap 'rm -f -- "$temporary_key"' RETURN
  curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 \
    "https://download.docker.com/linux/$DOCKER_DISTRO/gpg" -o "$temporary_key"
  install -m 0755 -d /etc/apt/keyrings
  install -m 0644 "$temporary_key" /etc/apt/keyrings/docker.asc
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$DOCKER_DISTRO
Suites: $DOCKER_SUITE
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
  docker info >/dev/null
  docker compose version
  echo 'Docker готов. Группы пользователей и TCP-доступ к daemon не изменялись.'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then install_docker; fi
