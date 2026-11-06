#!/usr/bin/env bash
# Requires Linux x86-64, Git, curl, sha256sum, tar, Docker, and network access.
# The Go toolchain and caches are isolated in a temporary directory.
# This source build has not yet been validated on the target cluster.
set -euo pipefail
cd "$(dirname "$0")/.."
archive=${1:-microservices-images.tar}
command -v git >/dev/null || { echo 'Git is required to fetch the pinned MinIO source.' >&2; exit 1; }
for dependency in curl sha256sum tar docker; do
  command -v "$dependency" >/dev/null || { echo "Required command not found: $dependency" >&2; exit 1; }
done
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo 'The pinned Go toolchain in this script requires Linux x86-64.' >&2
  exit 1
fi
for service in frontend security uploader; do
  docker build -t "microservices-${service}:local" "services/${service}"
done

# MinIO's 2025-10-15 security release, pinned to the full official commit.
# Its upstream Dockerfile expects a precompiled binary and makes /usr/bin
# world-writable. Instead, compile the same source with the upstream build
# tags and release ldflags, and run it in a non-root container.
# Clone into an isolated temporary directory; do not include upstream source
# or its Git metadata in this project's Git repository. Verify the complete
# commit, not only the movable release tag, before compiling.
minio_tmp=$(mktemp -d)
trap 'rm -rf -- "$minio_tmp"' EXIT
minio_src="$minio_tmp/minio"
GIT_TERMINAL_PROMPT=0 git clone --depth 1 --branch RELEASE.2025-10-15T17-29-55Z \
  https://github.com/minio/minio.git "$minio_src"
expected_commit=9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a
actual_commit=$(git -C "$minio_src" rev-parse HEAD)
if [[ "$actual_commit" != "$expected_commit" ]]; then
  echo 'MinIO source commit differs from the expected release; stopping.' >&2
  exit 1
fi
go_archive="$minio_tmp/go1.24.8.linux-amd64.tar.gz"
curl --fail --location --silent --show-error --retry 3 --connect-timeout 20 --max-time 300 \
  https://go.dev/dl/go1.24.8.linux-amd64.tar.gz -o "$go_archive"
printf '%s  %s\n' 6842c516ca66c89d648a7f1dbe28e28c47b61b59f8f06633eb2ceb1188e9251d "$go_archive" | sha256sum -c -
tar -xzf "$go_archive" -C "$minio_tmp"
mkdir -p "$minio_tmp/gopath" "$minio_tmp/gocache" "$minio_tmp/home" "$minio_tmp/runtime"
(
  cd "$minio_src"
  HOME="$minio_tmp/home" GOPATH="$minio_tmp/gopath" GOCACHE="$minio_tmp/gocache" \
    GOTOOLCHAIN=local GOMAXPROCS=1 CGO_ENABLED=0 "$minio_tmp/go/bin/go" build -p 1 \
    -tags kqueue -trimpath \
    -ldflags="-s -w -X github.com/minio/minio/cmd.Version=2025-10-15T17:29:55Z -X github.com/minio/minio/cmd.CopyrightYear=2025 -X github.com/minio/minio/cmd.ReleaseTag=RELEASE.2025-10-15T17-29-55Z -X github.com/minio/minio/cmd.CommitID=9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a -X github.com/minio/minio/cmd.ShortCommitID=9e49d5e7a648" \
    -o "$minio_tmp/runtime/minio" .
)
cp "$minio_src/LICENSE" "$minio_src/CREDITS" "$minio_src/NOTICE" "$minio_tmp/runtime/"
cat > "$minio_tmp/runtime/Dockerfile" <<'DOCKERFILE'
FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/minio/minio" \
      org.opencontainers.image.revision="9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a"
COPY minio /usr/local/bin/minio
COPY LICENSE CREDITS NOTICE /licenses/
ENV HOME=/tmp
USER 1000:1000
EXPOSE 9000
ENTRYPOINT ["/usr/local/bin/minio"]
CMD ["server", "/data"]
DOCKERFILE

# A local runtime context also works when the buildx plugin is absent.
DOCKER_BUILDKIT=0 docker build -t microservices-minio:local "$minio_tmp/runtime"

docker save -o "$archive" microservices-frontend:local microservices-security:local microservices-uploader:local microservices-minio:local
echo "Archive: $archive"
echo "After authorization, copy it over verified SSH and import on the K3s node:"
echo "  sudo k3s ctr -n k8s.io images import /path/to/microservices-images.tar"
echo "The archive contains the three application images and the source-built MinIO image."
