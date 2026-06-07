---
parent: Connecting to LLMs
nav_order: 100
---

# OpenAI

To work with OpenAI's models, you need to provide your
[OpenAI API key](https://help.openai.com/en/articles/4936850-where-do-i-find-my-secret-api-key)
either in the `OPENAI_API_KEY` environment variable or
via the `--api-key openai=<key>` command line switch.

First, install pyclaw:

{% include install.md %}

Then configure your API keys:

```
export OPENAI_API_KEY=<key> # Mac/Linux
setx   OPENAI_API_KEY <key> # Windows, restart shell after setx
```

Start working with pyclaw and OpenAI on your codebase:

```bash
# Change directory into your codebase
cd /to/your/project

# o3-mini
pyclaw --model o3-mini

# o1-mini
pyclaw --model o1-mini

# GPT-4o
pyclaw --model gpt-4o

# List models available from OpenAI
pyclaw --list-models openai/
```

You can use `pyclaw --model <model-name>` to use any other OpenAI model.
For example, if you want to use a specific version of GPT-4 Turbo
you could do `pyclaw --model gpt-4-0125-preview`.

## Reasoning models from other providers

Many of OpenAI's 
"reasoning" models have restrictions on streaming and setting the temperature parameter.
Some also support different levels of "reasoning effort".
Pyclaw is configured to work properly with these models
when served through major provider APIs and
has a `--reasoning-effort` setting.

You may need to [configure reasoning model settings](/docs/config/reasoning.html)
if you are using them through another provider
and see errors related to temperature or system prompt.
