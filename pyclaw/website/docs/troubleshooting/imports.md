---
parent: Troubleshooting
nav_order: 28
---

# Dependency versions

Pyclaw expects to be installed with the
correct versions of all of its required dependencies.

If you've been linked to this doc from a GitHub issue, 
or if pyclaw is reporting `ImportErrors`
it is likely that your
pyclaw install is using incorrect dependencies.


## Avoid package conflicts

If you are using pyclaw to work on a python project, sometimes your project will require
specific versions of python packages which conflict with the versions that pyclaw
requires.
If this happens, you may see errors like these when running pip installs:

```
pyclaw 0.23.0 requires somepackage==X.Y.Z, but you have somepackage U.W.V which is incompatible.
```

## Install with pyclaw-install, uv or pipx

If you are having dependency problems you should consider
[installing pyclaw using pyclaw-install, uv or pipx](/docs/install.html).
This will ensure that pyclaw is installed in its own python environment,
with the correct set of dependencies.

## Package managers like Homebrew, AUR, ports

Package managers often install pyclaw with the wrong dependencies, leading
to import errors and other problems.

It is recommended to
[install pyclaw using pyclaw-install, uv or pipx](/docs/install.html).


## Dependency versions matter

Pyclaw pins its dependencies and is tested to work with those specific versions.
If you are installing pyclaw directly with pip
you should be careful about upgrading or downgrading the python packages that
pyclaw uses.

In particular, be careful with the packages with pinned versions 
noted at the end of
[pyclaw's requirements.in file](https://github.com/Pyclaw-AI/pyclaw/blob/main/requirements/requirements.in).
These versions are pinned because pyclaw is known not to work with the
latest versions of these libraries.

Also be wary of upgrading `litellm`, as it changes versions frequently
and sometimes introduces bugs or backwards incompatible changes.

## Replit

{% include replit-pipx.md %}
