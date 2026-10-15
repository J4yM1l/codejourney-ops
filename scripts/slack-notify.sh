#!/usr/bin/env bash
# Post one Block Kit message to Slack #deploys (homelab-infra ADR-030, unit S2).
#
#   SLACK_WEBHOOK_URL   incoming webhook (secret of environment slack-notify)
#   STATE               waiting | written | released | failed | rejected | info
#   TITLE               one line, plain text
#   BODY                Slack mrkdwn, may span lines
#
# The run link is added from GITHUB_SERVER_URL / GITHUB_REPOSITORY / GITHUB_RUN_ID.
# The JSON is built by jq (no string pasting, so untrusted text cannot break it) and the
# webhook URL reaches curl through its config on stdin, never argv or the log.
# A failed post prints a warning and exits 0: Slack is a copy, never a release gate.
set -euo pipefail

: "${SLACK_WEBHOOK_URL:?}" "${STATE:?}" "${TITLE:?}" "${BODY:?}"

case "${STATE}" in
  waiting)  emoji="⏳" ;;
  written)  emoji="📝" ;;
  released) emoji="✅" ;;
  failed)   emoji="❌" ;;
  rejected) emoji="🚫" ;;
  *)        emoji="ℹ️" ;;
esac
run_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"

payload="$(jq -n \
  --arg text "${emoji} ${TITLE}" \
  --arg body "${BODY}" \
  --arg run "<${run_url}|${GITHUB_WORKFLOW} run ${GITHUB_RUN_ID}>" \
  '{
     text: $text,
     blocks: [
       {type: "section", text: {type: "mrkdwn", text: ("*" + $text + "*")}},
       {type: "section", text: {type: "mrkdwn", text: $body}},
       {type: "context", elements: [{type: "mrkdwn", text: $run}]}
     ]
   }')"

if ! printf 'url = "%s"\n' "${SLACK_WEBHOOK_URL}" \
    | curl -sS --fail-with-body --max-time 15 -K - \
        -H 'Content-Type: application/json' --data "${payload}"; then
  echo "::warning::Slack notification failed (see above); the release itself is not affected"
fi
echo
