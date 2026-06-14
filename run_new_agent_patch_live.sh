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

cat > "$tmpdir/math_utils.py" <<'PY'
def factorial(n):
    # TODO: implement
    pass
PY

echo
echo "Before:"
cat "$tmpdir/math_utils.py"

echo
echo "Running NEW agent-patch path with gpt-4o..."

python -m apps.cli.app \
  --provider litellm \
  --model openai/gpt-4o \
  agent-patch \
  --workspace "$tmpdir" \
  --target-path math_utils.py \
  --prompt 'Modify only math_utils.py. Implement factorial(n). It should return 1 for n == 0 or n == 1, compute n! for positive integers, and raise ValueError("n must be non-negative") for negative input.'

echo
echo "After:"
cat "$tmpdir/math_utils.py"

echo
echo "Syntax check:"
python -m py_compile "$tmpdir/math_utils.py"

echo
echo "Behavior check:"
PYTHONPATH="$tmpdir" python - <<'PY'
from math_utils import factorial

assert factorial(0) == 1
assert factorial(1) == 1
assert factorial(5) == 120

try:
    factorial(-1)
except ValueError as e:
    assert str(e) == "n must be non-negative"
else:
    raise AssertionError("factorial(-1) should raise ValueError")

print("Manual tests passed.")
PY

echo
echo "Done. Workspace kept at:"
echo "$tmpdir"
