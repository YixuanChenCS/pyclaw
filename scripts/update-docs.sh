#!/bin/bash

# exit when any command fails
set -e

if [ -z "$1" ]; then
  ARG=-r
else
  ARG=$1
fi

if [ "$ARG" != "--check" ]; then
  tail -1000 ~/.pyclaw/analytics.jsonl > pyclaw/website/assets/sample-analytics.jsonl
  cog -r pyclaw/website/docs/faq.md
fi

# README.md before index.md, because index.md uses cog to include README.md
cog $ARG \
    README.md \
    pyclaw/website/index.html \
    pyclaw/website/HISTORY.md \
    pyclaw/website/docs/usage/commands.md \
    pyclaw/website/docs/languages.md \
    pyclaw/website/docs/config/dotenv.md \
    pyclaw/website/docs/config/options.md \
    pyclaw/website/docs/config/pyclaw_conf.md \
    pyclaw/website/docs/config/adv-model-settings.md \
    pyclaw/website/docs/config/model-aliases.md \
    pyclaw/website/docs/leaderboards/index.md \
    pyclaw/website/docs/leaderboards/edit.md \
    pyclaw/website/docs/leaderboards/refactor.md \
    pyclaw/website/docs/llms/other.md \
    pyclaw/website/docs/more/infinite-output.md \
    pyclaw/website/docs/legal/privacy.md
