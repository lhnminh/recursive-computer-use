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

Available models (via proxy): `gpt-5.6-sol` (default), `gpt-5.5`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-6-astra`.

Coordinate guide replay is experimental and disabled by default because page
changes can make captured clicks unreliable. Use `--guides` to opt in; use
`--relearn` to force free navigation and replace a guide, or `--no-guides` to
disable guide lookup and capture explicitly.
