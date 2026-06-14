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

cat > "$tmpdir/test_math_utils.py" <<'PY'
import pytest
from math_utils import factorial

def test_factorial_basic():
    assert factorial(0) == 1
    assert factorial(1) == 1
    assert factorial(5) == 120

def test_factorial_negative():
    with pytest.raises(ValueError, match="n must be non-negative"):
        factorial(-1)
PY

echo
echo "Before:"
cat "$tmpdir/math_utils.py"

echo
echo "Running current pyclaw CLI with gpt-4o..."

cd "$tmpdir"

pyclaw \
  --model openai/gpt-4o \
  --yes-always \
  --no-auto-commits \
  --message 'Modify only math_utils.py. Implement factorial(n). It should return 1 for n == 0 or n == 1, compute n! for positive integers, and raise ValueError("n must be non-negative") for negative input. Do not modify test_math_utils.py.' \
  math_utils.py

echo
echo "After:"
cat "$tmpdir/math_utils.py"

echo
echo "Syntax check:"
python -m py_compile "$tmpdir/math_utils.py"

echo
echo "Behavior check:"
if command -v pytest >/dev/null 2>&1; then
  pytest -q "$tmpdir/test_math_utils.py"
else
  python - <<'PY'
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
fi

echo
echo "Done. Workspace kept at:"
echo "$tmpdir"
