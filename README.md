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
successful run. Guides are written to the MongoDB `guides` collection in the
database configured by `MONGODB_DB` (default `recursive_computer_use`), as well
as to a local JSON fallback at `~/.recursive_computer_use/guides.json`. Set
`MONGODB_URI` to your MongoDB connection string; if MongoDB is unavailable, the
run still completes and the guide is saved locally. Private LinkedIn text is
stored as a placeholder. Use `--replay-guides` to replay a matching guide,
filling the site and requested text from the current prompt; without that flag,
runs use screenshot-guided navigation.

Example with MongoDB Atlas:

```dotenv
MONGODB_URI=mongodb+srv://<username>:<password>@<cluster-host>/?retryWrites=true&w=majority
MONGODB_DB=recursive_computer_use
```

Then capture a guide after a successful task:

```bash
uv run recursive-computer-use --guides --site example.com --task search "search example.com for computer use"
```

The guide document contains the site/task/environment key, ordered action
steps, model, timestamps, and success/failure counters. It does not store the
full run prompt or action history; those are in the existing `runs` and
`actions` collections.
