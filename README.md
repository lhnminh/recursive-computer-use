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
uv run recursive-computer-use --model gpt-5.6-sol "click the search box and type hello"
```

Available models (via proxy): `gpt-5.5` (default), `gpt-5.6-sol`, `gpt-6-astra`.
