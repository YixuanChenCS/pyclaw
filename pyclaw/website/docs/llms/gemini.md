---
parent: Connecting to LLMs
nav_order: 300
---

# Gemini

You'll need a [Gemini API key](https://aistudio.google.com/app/u/2/apikey).

First, install pyclaw:

{% include install.md %}

Then configure your API keys:

```bash
export GEMINI_API_KEY=<key> # Mac/Linux
setx   GEMINI_API_KEY <key> # Windows, restart shell after setx
```

Start working with pyclaw and Gemini on your codebase:


```bash
# Change directory into your codebase
cd /to/your/project

# You can run the Gemini 2.5 Pro model with this shortcut:
pyclaw --model gemini

# You can run the Gemini 2.5 Pro Exp for free, with usage limits:
pyclaw --model gemini-exp

# List models available from Gemini
pyclaw --list-models gemini/
```

