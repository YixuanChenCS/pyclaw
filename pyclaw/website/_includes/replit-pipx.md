To use pyclaw with pipx on replit, you can run these commands in the replit shell:

```bash
pip install pipx
pipx run pyclaw ...normal pyclaw args...
```

If you install pyclaw with pipx on replit and try and run it as just `pyclaw` it will crash with a missing `libstdc++.so.6` library.

