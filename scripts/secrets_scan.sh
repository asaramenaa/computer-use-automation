#!/usr/bin/env bash
# Pre-push secrets scan over tracked/untracked files that would be committed (respects .gitignore).
# Exit 1 if anything suspicious is found. Run from the repo root.
set -euo pipefail
cd "$(dirname "$0")/.."
files=$(git ls-files --cached --others --exclude-standard 2>/dev/null || find . -type f -not -path './.venv/*' -not -path './.git/*' -not -path './runs/*')
status=0
check() {  # $1 label, $2 regex
  hits=$(echo "$files" | xargs grep -nIE "$2" 2>/dev/null | grep -v -E '^(\./)?(scripts/secrets_scan\.sh|\.env\.example)' || true)
  if [ -n "$hits" ]; then echo "!! $1"; echo "$hits" | head -20; status=1; fi
}
check "Anthropic key"            'sk-ant-[A-Za-z0-9_-]{10,}'
check "Google API key"           'AIza[0-9A-Za-z_-]{30,}'
check "OpenAI key"               'sk-[A-Za-z0-9]{32,}'
check "Groq key"                 'gsk_[A-Za-z0-9]{20,}'
check "private key block"        'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY'
check "bearer token"             'Bearer [A-Za-z0-9._-]{20,}'
# The seeded fake password may appear in target_app/, tests/, README.md and .env.example only.
bad=$(echo "$files" | xargs grep -lF 'Passw0rd!' 2>/dev/null | grep -v -E '^(src/cuc/target_app/|tests/|README\.md|\.env\.example|scripts/secrets_scan\.sh)' || true)
if [ -n "$bad" ]; then echo "!! seeded password leaked into: $bad"; status=1; fi
ssn=$(echo "$files" | grep -E '^(evidence|artifacts|app_profiles|schema)/' | xargs grep -nE '\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b' 2>/dev/null || true)
if [ -n "$ssn" ]; then echo "!! SSN-shaped value in shipped evidence/artifacts"; echo "$ssn" | head; status=1; fi
acct=$(echo "$files" | grep -E '^(evidence|artifacts)/' | xargs grep -nE '\b[0-9]{9,17}\b' 2>/dev/null | grep -v -E '"(ts|recorded_at|started_at|finished_at)"' || true)
if [ -n "$acct" ]; then echo "!! account-number-shaped value in shipped evidence/artifacts (check redaction)"; echo "$acct" | head; status=1; fi
if [ -f .env ]; then git check-ignore -q .env || { echo "!! .env is not gitignored"; status=1; }; fi
[ $status -eq 0 ] && echo "secrets scan: clean" || echo "secrets scan: FINDINGS ABOVE"
exit $status
