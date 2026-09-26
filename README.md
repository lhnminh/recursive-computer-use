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

Run with `--verbose` to see turn-by-turn activity. Guide lookup, replay, and
learning are enabled by default:

```bash
uv run recursive-computer-use --verbose "your task here"
```

Choose a model with `--model`:

```bash
uv run recursive-computer-use --model gpt-6-astra "click the search box and type hello"
```

Available models (via proxy): `gpt-5.6-terra` (default), `gpt-5.5`, `gpt-5.6-luna`, `gpt-6-astra`.

Guide lookup, replay, and capture are enabled by default. Before navigating,
the agent looks for a matching guide and replays it when available. If there is
no guide, it navigates from screenshots and saves the successful actions as a
guide. If a replay fails, it falls back to screenshot-guided navigation and
replaces the guide with the successful new run. Use `--no-guides` to disable
all guide lookup, replay, and capture. `--guides` and `--replay-guides` remain
available as explicit reminders of each behavior. Guides are written to the
MongoDB `guides` collection in the
database configured by `MONGODB_DB` (default `recursive_computer_use`), as well
as to a local JSON fallback at `~/.recursive_computer_use/guides.json`. Set
`MONGODB_URI` to your MongoDB connection string; if MongoDB is unavailable, the
run still completes and the guide is saved locally. Private LinkedIn text is
stored as a placeholder. Replay fills the site and requested text from the
current prompt.

Example with MongoDB Atlas:

```dotenv
MONGODB_URI=mongodb+srv://<username>:<password>@<cluster-host>/?retryWrites=true&w=majority
MONGODB_DB=recursive_computer_use
```

The site is inferred from the prompt, including named services such as Reddit.
The task key currently uses the prompt text, so repeat the same prompt to reuse
the same guide. The first successful run records it; later runs replay it, and
a failed replay falls back to screenshots and replaces the guide after success.
For example:

```bash
uv run recursive-computer-use --verbose "On Reddit, draft a post saying hello"
```

The guide document contains the site/task/environment key, ordered action
steps, model, timestamps, and success/failure counters. It does not store the
full run prompt or action history; those are in the existing `runs` and
`actions` collections.
