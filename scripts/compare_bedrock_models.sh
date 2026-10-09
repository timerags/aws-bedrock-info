#!/usr/bin/env bash
#
# Fetch the list of Amazon Bedrock foundation models available in two regions
# and report which models are common, and which are only in one region.
#
# Usage:
#   ./scripts/compare_bedrock_models.sh [region1] [region2]
#
# Defaults: region1=ap-northeast-1, region2=us-east-1
#
# Requires: aws cli (configured credentials), jq

set -euo pipefail

REGION1="${1:-ap-northeast-1}"
REGION2="${2:-us-east-1}"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

fetch_models() {
    local region="$1"
    local outfile="$2"
    aws bedrock list-foundation-models --region "$region" --output json > "$outfile"
}

echo "Fetching models for $REGION1..." >&2
fetch_models "$REGION1" "$WORKDIR/region1.json"

echo "Fetching models for $REGION2..." >&2
fetch_models "$REGION2" "$WORKDIR/region2.json"

jq -r '.modelSummaries[].modelId' "$WORKDIR/region1.json" | sort -u > "$WORKDIR/region1_ids.txt"
jq -r '.modelSummaries[].modelId' "$WORKDIR/region2.json" | sort -u > "$WORKDIR/region2_ids.txt"

COUNT1=$(wc -l < "$WORKDIR/region1_ids.txt" | tr -d ' ')
COUNT2=$(wc -l < "$WORKDIR/region2_ids.txt" | tr -d ' ')
COMMON_COUNT=$(comm -12 "$WORKDIR/region1_ids.txt" "$WORKDIR/region2_ids.txt" | wc -l | tr -d ' ')

echo ""
echo "=== Summary ==="
echo "$REGION1: $COUNT1 models"
echo "$REGION2: $COUNT2 models"
echo "Common to both: $COMMON_COUNT models"

echo ""
echo "=== Only in $REGION1 (not in $REGION2) ==="
comm -23 "$WORKDIR/region1_ids.txt" "$WORKDIR/region2_ids.txt"

echo ""
echo "=== Only in $REGION2 (not in $REGION1) ==="
comm -13 "$WORKDIR/region1_ids.txt" "$WORKDIR/region2_ids.txt"
