#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

load_env() {
  if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    . "$REPO_ROOT/.env"
    set +a
  fi 

  if [ -z "${ECR_REPO:-}" ]; then
    echo "ERROR: ECR_REPO is not set (export it or add it to .env)" >&2
    exit 1
  fi

  AWS_REGION="${AWS_REGION}"
  ECR_REPO="${ECR_REPO}"
}

require_account_id() {
  if [ -z "${AWS_ACCOUNT_ID:-}" ]; then
    echo "ERROR: AWS_ACCOUNT_ID is not set (export it or add it to .env)" >&2
    exit 1
  fi
}

ensure_aws_cli() {
  if ! command -v aws >/dev/null 2>&1; then
    echo "ERROR: AWS CLI not found. Install it first: https://aws.amazon.com/cli/" >&2
    exit 1
  fi
}

ecr_registry() {
  echo "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
}

ecr_login() {
  local registry
  registry="$(ecr_registry)"
  echo "Logging into ECR: ${registry}"
  echo "Ensure you have run aws configure"
  aws ecr get-login-password --region "$AWS_REGION" |
    docker login --username AWS --password-stdin "$registry"
}

ensure_repository() {
  local repo="$1"
  if aws ecr describe-repositories --region "$AWS_REGION" \
    --repository-names "$repo" >/dev/null 2>&1; then
    echo "Repository exists: ${repo}"
  else
    echo "Creating repository: ${repo}"
    aws ecr create-repository --region "$AWS_REGION" \
      --repository-name "$repo" >/dev/null
  fi
}

push_image() {
  local tag="$1"
  shift
  local image
  image="$(ecr_registry)/$ECR_REPO:$tag"

  echo "Building image ${image}..."
  docker build --platform linux/amd64 "$@" -t "$image" -f "$DOCKERFILE" "$BUILD_CONTEXT"


  echo "Pushing ${image}"
  docker push "$image"

  echo "Done."
}
 