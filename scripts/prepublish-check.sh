#!/usr/bin/env bash
# Fails if anything private is tracked by git or sits in the history that would be pushed.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.."
bad=0
pat='(^|/)(\.env|data/.*|.*\.(db|sqlite|sqlite3|eml|mbox|xlsx|xls|pdf|csv))$'
tracked=$(git ls-files | grep -E "$pat" | grep -v '^\.env\.example$' || true)
[ -n "$tracked" ] && { echo "TRACKED private-looking files:"; echo "$tracked"; bad=1; }
hist=$(git log --all --name-only --pretty=format: 2>/dev/null | grep -E "$pat" | grep -v '^\.env\.example$' | sort -u || true)
[ -n "$hist" ] && { echo "In HISTORY (deleting them now is not enough, start a fresh repo):"; echo "$hist"; bad=1; }
sec=$(git grep -nIE '^(IMAP_PASSWORD|IMAP_USER)=.+@|^IMAP_PASSWORD=[^ ]+' -- . ':!.env.example' ':!deploy/env.example' ':!tests' ':!README.md' ':!SETUP.md' 2>/dev/null || true)
[ -n "$sec" ] && { echo "Possible credentials:"; echo "$sec"; bad=1; }
[ $bad = 0 ] && echo "OK: nothing private is tracked." || exit 1
