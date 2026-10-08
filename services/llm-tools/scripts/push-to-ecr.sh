
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib-ecr.sh"

load_env
require_account_id
ensure_aws_cli
ecr_login
ensure_repository "$ECR_REPO"

DOCKERFILE="$REPO_ROOT/Dockerfile"
BUILD_CONTEXT="$REPO_ROOT/"

push_image summarise