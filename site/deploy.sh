#!/usr/bin/env bash
# Deploy tryambient.app (static page in ./public + waitlist Worker in ./src, D1 `ambient-waitlist`).
#   ./site/deploy.sh          needs `npx wrangler login` once
# Signups:  npx wrangler d1 execute ambient-waitlist --remote --command "select * from waitlist order by id desc"
set -euo pipefail
cd "$(dirname "$0")"
npx --yes wrangler@4 d1 migrations apply ambient-waitlist --remote
npx --yes wrangler@4 deploy
