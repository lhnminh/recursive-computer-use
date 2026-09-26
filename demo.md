# LinkedIn post-drafting demo

This demo shows the computer-use agent navigating LinkedIn and preparing a post
for review. It must leave the post as a draft in the composer and must not click
**Post**, **Share**, or any other publish control.

## Run

Sign in to LinkedIn in the browser first, then run:

```bash
uv run recursive-computer-use --verbose \
  "Use the browser address bar to open https://www.linkedin.com/ (Command+L on macOS, Ctrl+L on Windows/Linux; do not type the URL into a webpage field). Wait and verify LinkedIn has loaded from a screenshot. Then prepare this text as a post, leaving it open for my review without publishing it: Testing a computer-use agent workflow: navigating LinkedIn, preparing a post, and leaving it ready for review. Nothing is published automatically."
```

## What the agent should do

1. Inspect the current screen. Focus the browser address bar with Command+L on
   macOS or Ctrl+L on Windows/Linux, enter `https://www.linkedin.com/`, and
   press Enter. Do not type the URL into a webpage field.
2. Wait, take a fresh screenshot, and verify LinkedIn loaded before clicking or
   typing anywhere in the page.
3. Open the post composer and enter the supplied text. If any text appears in
   the wrong place, stop typing, inspect the screen, and recover before
   continuing.
4. Confirm from the visible screen that the text is in the composer, then stop
   with the composer open for review. Report only what the screenshot confirms.

The agent must not publish or share the post. The run's LinkedIn interaction
guide may be saved locally with coordinates and action types, but typed text and
the original prompt are omitted. On a later matching run in the same screen
environment, the saved guide is replayed in fast mode; its blank text step is a
no-op, so the agent does not reuse the previous post text.
