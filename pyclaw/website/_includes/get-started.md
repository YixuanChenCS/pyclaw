
If you already have python 3.8-3.13 installed, you can get started quickly like this.

First, install pyclaw:

{% include install.md %}

Start working with pyclaw on your codebase:

```bash
# Change directory into your codebase
cd /to/your/project

# DeepSeek
pyclaw --model deepseek --api-key deepseek=<key>

# Claude 3.7 Sonnet
pyclaw --model sonnet --api-key anthropic=<key>

# o3-mini
pyclaw --model o3-mini --api-key openai=<key>
```
