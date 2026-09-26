# recursive-computer-use

AI agent that controls your desktop using pyautogui and an OpenAI-compatible model.

## Setup

**1. Start the local model proxy** (in a separate terminal):

```bash
npx codex-as-api
```

**2. Set your API key:**

```bash
cp .env.example .env
# edit .env and fill in your OPENAI_API_KEY
```

## Run

```bash
uv run recursive-computer-use "open Safari and go to openai.com"
```

Add `--verbose` to see turn-by-turn activity:

```bash
uv run recursive-computer-use --verbose "take a screenshot and describe what's on screen"
```

Choose a model with `--model`:

```bash
uv run recursive-computer-use --model gpt-6-astra "click the search box and type hello"
```

Available models (via proxy): `gpt-5.6-terra` (default), `gpt-5.5`, `gpt-5.6-luna`, `gpt-6-astra`.

Guide capture is disabled by default. Use `--guides` to save the actions from a
successful run. Private LinkedIn text is stored as a placeholder. Use
`--replay-guides` to replay a matching guide, filling the site and requested
text from the current prompt; without that flag, runs use screenshot-guided
navigation.
