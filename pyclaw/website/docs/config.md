---
nav_order: 55
has_children: true
description: Information on all of pyclaw's settings and how to use them.
---

# Configuration

Pyclaw has many options which can be set with
command line switches.
Most options can also be set in an `.pyclaw.conf.yml` file
which can be placed in your home directory or at the root of
your git repo. 
Or by setting environment variables like `PYCLAW_xxx`
either in your shell or a `.env` file.

Here are 4 equivalent ways of setting an option. 

With a command line switch:

```
$ pyclaw --dark-mode
```

Using a `.pyclaw.conf.yml` file:

```yaml
dark-mode: true
```

By setting an environment variable:

```
export PYCLAW_DARK_MODE=true
```

Using an `.env` file:

```
PYCLAW_DARK_MODE=true
```

{% include keys.md %}

