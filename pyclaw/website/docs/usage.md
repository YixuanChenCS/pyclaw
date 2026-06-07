---
nav_order: 30
has_children: true
description: How to use pyclaw to pair program with AI and edit code in your local git repo.
---

# Usage

Run `pyclaw` with the source code files you want to edit.
These files will be "added to the chat session", so that
pyclaw can see their
contents and edit them for you.
They can be existing files or the name of files you want
pyclaw to create for you.

```
pyclaw <file1> <file2> ...
```

At the pyclaw `>` prompt, ask for code changes and pyclaw
will edit those files to accomplish your request.


```
$ pyclaw factorial.py

Pyclaw v0.37.1-dev
Models: gpt-4o with diff edit format, weak model gpt-3.5-turbo
Git repo: .git with 258 files
Repo-map: using 1024 tokens
Use /help to see in-chat commands, run with --help to see cmd line args
───────────────────────────────────────────────────────────────────────
> Make a program that asks for a number and prints its factorial

...
```

{% include help-tip.md %}

## Adding files

To edit files, you need to "add them to the chat".
Do this
by naming them on the pyclaw command line.
Or, you can use the in-chat
`/add` command to add files.


Only add the files that need to be edited for your task.
Don't add a bunch of extra files.
If you add too many files, the LLM can get overwhelmed
and confused (and it costs more tokens).
Pyclaw will automatically
pull in content from related files so that it can
[understand the rest of your code base](https://pyclaw.chat/docs/repomap.html).

You can use pyclaw without adding any files,
and it will try to figure out which files need to be edited based
on your requests.

{: .tip }
You'll get the best results if you think about which files need to be
edited. Add **just** those files to the chat. Pyclaw will include
relevant context from the rest of your repo.

## LLMs

{% include works-best.md %}

```
# o3-mini
$ pyclaw --model o3-mini --api-key openai=<key>

# Claude 3.7 Sonnet
$ pyclaw --model sonnet --api-key anthropic=<key>
```

Or you can run `pyclaw --model XXX` to launch pyclaw with
another model.
During your chat you can switch models with the in-chat
`/model` command.

## Making changes

Ask pyclaw to make changes to your code.
It will show you some diffs of the changes it is making to
complete you request.
[Pyclaw will git commit all of its changes](/docs/git.html),
so they are easy to track and undo.

You can always use the `/undo` command to undo AI changes that you don't
like.
