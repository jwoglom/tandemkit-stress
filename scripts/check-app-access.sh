#!/bin/bash
# Whether the GitHub App token in $GH_TOKEN can read each owner/repo given, with an annotation naming the fix when not.
# git reports a token without Contents access to a private repository only as "Repository not found".
set -uo pipefail
status=0
for repo in "$@"; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $GH_TOKEN" \
           -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$repo/commits?per_page=1")
  if [ "$code" = 200 ]; then
    echo "ok: the App token can read $repo"
  else
    echo "::error title=GitHub App cannot read $repo::HTTP $code. The App behind TRIO_E2E_APP_ID needs Contents: read on $repo: set it under the App's Permissions, accept the change on its installation, and keep $repo among the installation's repositories."
    status=1
  fi
done
exit $status
