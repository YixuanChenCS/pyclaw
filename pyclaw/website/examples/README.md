---
title: Example chat transcripts
has_children: true
nav_order: 80
has_toc: false
---

# Example chat transcripts

Below are some chat transcripts showing what it's like to code with pyclaw.
In the chats, you'll see a variety of coding tasks like generating new code, editing existing code, debugging, exploring unfamiliar code, etc.

* [**Hello World Flask App**](https://pyclaw.chat/examples/hello-world-flask.html): Start from scratch and have pyclaw create a simple Flask app with various endpoints, such as adding two numbers and calculating the Fibonacci sequence.

* [**Javascript Game Modification**](https://pyclaw.chat/examples/2048-game.html): Dive into an existing open-source repo, and get pyclaw's help to understand it and make modifications.

* [**Complex Multi-file Change with Debugging**](https://pyclaw.chat/examples/complex-change.html): Pyclaw makes a complex code change that is coordinated across multiple source files, and resolves bugs by reviewing error output and doc snippets.

* [**Create a Black Box Test Case**](https://pyclaw.chat/examples/add-test.html): Pyclaw creates a "black box" test case without access to the source of the method being tested, using only a [high level map of the repository based on ctags](https://pyclaw.chat/docs/ctags.html).

* [**Honor the NO_COLOR env var**](https://pyclaw.chat/examples/no-color.html): The user pastes the NO_COLOR spec from no-color.org into the chat, and pyclaw modifies the application to conform.

* [**Download, analyze and plot US Census data**](https://pyclaw.chat/examples/census.html): Pyclaw downloads census data, suggests some hypotheses to test, tests one and then summarizes and plots a graph of the results.

* [**Semantic Search & Replace**](semantic-search-replace.md): Updating a collection of function calls, which requires dealing with various formatting and semantic differences in the various function call sites.

* [**Pong Game with Pygame**](pong.md): Creating a simple Pong game using the Pygame library, with customizations for paddle size and color, and ball speed adjustments.

* [**CSS Exercise: Animation Dropdown Menu**](css-exercises.md): A small CSS exercise involving adding animation to a dropdown menu.

* [**Automatically Update Docs**](update-docs.md): Automatically updating documentation based on the latest version of the main() function.

* [**Editing an Asciinema Cast File**](asciinema.md): Editing escape sequences in an `asciinema` screencast file.

## What's happening in these chats?

To better understand the chat transcripts, it's worth knowing that:

  - Each time the LLM suggests a code change, `pyclaw` automatically applies it to the source files.
  - After applying the edits, `pyclaw` commits them to git with a descriptive commit message.
  - The LLM can only see and edit files which have been "added to the chat session". The user adds files either via the command line or the in-chat `/add` command. If the LLM asks to see specific files, `pyclaw` asks the user for permission to add them to the chat. The transcripts contain notifications from `pyclaw` whenever a file is added or dropped from the session.

## Transcript formatting

<div class="chat-transcript" markdown="1">

> This is output from the pyclaw tool.

#### These are chat messages written by the user.

Chat responses from the LLM are in a blue font like this, and often include colorized "edit blocks" that specify edits to the code.
Here's a sample edit block that switches from printing "hello" to "goodbye":

```python
hello.py
<<<<<<< ORIGINAL
print("hello")
=======
print("goodbye")
>>>>>>> UPDATED
```

</div>
