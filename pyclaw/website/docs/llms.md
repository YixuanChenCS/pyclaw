---
title: Connecting to LLMs
nav_order: 40
has_children: true
description: Pyclaw can connect to most LLMs for AI pair programming.
---

# Pyclaw can connect to most LLMs
{: .no_toc }

[![connecting to many LLMs](/assets/llms.jpg)](https://pyclaw.chat/assets/llms.jpg)


## Best models
{: .no_toc }

Pyclaw works best with these models, which are skilled at editing code:

- [Gemini 2.5 Pro](/docs/llms/gemini.html)
- [DeepSeek R1 and V3](/docs/llms/deepseek.html)
- [Claude 3.7 Sonnet](/docs/llms/anthropic.html)
- [OpenAI o3, o4-mini and GPT-4.1](/docs/llms/openai.html)


## Free models
{: .no_toc }

Pyclaw works with a number of **free** API providers:

- [OpenRouter offers free access to many models](https://openrouter.ai/models/?q=free), with limitations on daily usage.
- Google's [Gemini 2.5 Pro Exp](/docs/llms/gemini.html) works very well with pyclaw.

## Local models
{: .no_toc }

Pyclaw can work also with local models, for example using [Ollama](/docs/llms/ollama.html).
It can also access
local models that provide an
[Open AI compatible API](/docs/llms/openai-compat.html).

## Use a capable model
{: .no_toc }

Check
[Pyclaw's LLM leaderboards](https://pyclaw.chat/docs/leaderboards/)
to see which models work best with pyclaw.

Be aware that pyclaw may not work well with less capable models.
If you see the model returning code, but pyclaw isn't able to edit your files
and commit the changes...
this is usually because the model isn't capable of properly
returning "code edits".
Models weaker than GPT 3.5 may have problems working well with pyclaw.

