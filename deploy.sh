#!/usr/bin/env bash
# Build the Lambda zip from lambda_function.py + requirements.txt and upload it.
#
#   ./deploy.sh            build and deploy
#   ./deploy.sh --build    build only (writes build/workout-api.zip)
#
# Uses whatever AWS credentials the CLI already has (e.g. `aws login`).
set -euo pipefail

FUNCTION_NAME="${FUNCTION_NAME:-workout-api}"
REGION="${AWS_REGION:-us-east-2}"
# Must match the Lambda's runtime and architecture.
PY_VERSION="3.13"
PLATFORM="manylinux2014_x86_64"

cd "$(dirname "$0")"
rm -rf build
mkdir -p build/package

python3 -m pip install --quiet --upgrade \
  --requirement requirements.txt \
  --target build/package \
  --platform "$PLATFORM" --python-version "$PY_VERSION" \
  --implementation cp --only-binary=:all:

cp lambda_function.py build/package/
rm -rf build/package/bin
find build/package -name __pycache__ -type d -prune -exec rm -rf {} +
python3 -c 'import shutil; shutil.make_archive("build/workout-api", "zip", "build/package")'
echo "Built build/workout-api.zip ($(du -h build/workout-api.zip | cut -f1))"

[[ "${1:-}" == "--build" ]] && exit 0

aws lambda update-function-code \
  --region "$REGION" \
  --function-name "$FUNCTION_NAME" \
  --zip-file fileb://build/workout-api.zip \
  --query '{Function:FunctionName,Modified:LastModified,Sha256:CodeSha256}' \
  --output table
aws lambda wait function-updated --region "$REGION" --function-name "$FUNCTION_NAME"
echo "Deployed $FUNCTION_NAME."
