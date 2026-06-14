#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "OPENAI_API_KEY is not set."
  echo "Paste your OpenAI API key now. It will not be shown:"
  read -rs OPENAI_API_KEY
  export OPENAI_API_KEY
  echo
fi

tmpdir="$(mktemp -d)"
echo "Temp workspace: $tmpdir"

cat > "$tmpdir/string_utils.py" <<'PY'
def is_palindrome(s):
    # TODO: implement
    pass
PY

echo
echo "Before:"
cat "$tmpdir/string_utils.py"

echo
echo "Running NEW agent-patch path with gpt-4o..."

python -m apps.cli.app \
  --provider litellm \
  --model openai/gpt-4o \
  agent-patch \
  --workspace "$tmpdir" \
  --target-path string_utils.py \
  --prompt 'Modify only string_utils.py. Implement is_palindrome(s). It should ignore case and ignore all non-alphanumeric characters. Return True if the cleaned string is a palindrome, otherwise return False. Empty strings should return True.'

echo
echo "After:"
cat "$tmpdir/string_utils.py"

echo
echo "Syntax check:"
python -m py_compile "$tmpdir/string_utils.py"

echo
echo "Behavior check:"
PYTHONPATH="$tmpdir" python - <<'PY'
from string_utils import is_palindrome

assert is_palindrome("") is True
assert is_palindrome("a") is True
assert is_palindrome("A man, a plan, a canal: Panama") is True
assert is_palindrome("race a car") is False
assert is_palindrome("No lemon, no melon") is True
assert is_palindrome("12321") is True
assert is_palindrome("1231") is False

print("Manual tests passed.")
PY

echo
echo "Done. Workspace kept at:"
echo "$tmpdir"
