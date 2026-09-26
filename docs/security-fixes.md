# Security fixes: how to apply them

A security review on 2026-09-26 found six problems. This file explains each
one and how to fix it. The fixes touch files that other people own, so no code
is committed yet. Each owner applies their own part.

A tested patch for all six is in `docs/security-fixes.patch`. It applies
cleanly to `main` as of this commit. With it, the full test suite passes
(`OK`, 3 skipped).

```bash
git apply docs/security-fixes.patch                       # everything
git apply --include='demo/*' docs/security-fixes.patch    # one area only
```

If your file has uncommitted work, commit it first. Then use
`git apply --3way` so git merges around your changes.

| # | Severity | Problem | File | Owner |
|---|---|---|---|---|
| 1 | Critical | Model code escapes the sandbox | `sandbox.py` | Minh |
| 2 | High | Any website can fake verifier results | `demo/app.py` | Codex |
| 3 | Medium | Some pyautogui calls skip policy and recording | `sandbox.py` | Minh |
| 4 | Medium | Dashboard: unescaped HTML and DNS rebinding | `dashboard/app.py` | Codex |
| 5 | Medium | Atlas lessons go into the system prompt unchecked | `evolution/runtime.py`, `agent.py` | Claude, Minh |
| 6 | Low | `test_store` fails on `main` | `tests/test_store.py` | Minh |

## 1. Model code escapes the sandbox

**Problem.** `Sandbox.run()` executes model-written Python in our own process.
It removes only six builtins. Each of these lines reached the real `os`
module or `open`:

```python
pyautogui._pyautogui.os.system("...")        # proxy exposes the real module
import json; json.codecs.builtins.open       # modules expose their imports
import re; re.enum.sys.modules["os"]
().__class__.__base__.__subclasses__()       # classic dunder walk
(i for i in []).gi_frame.f_back.f_globals    # frame walk, no underscores
```

The model reads the screen. Text on any web page can steer it to write this
code. That is remote code execution on the operator's machine.

**Fix.** Add four guards in `sandbox.py`:

1. Parse the code with `ast` before `exec`. Reject any name or attribute that
   starts with `_`. Also reject the frame attributes `gi_frame`, `cr_frame`,
   `ag_frame`, `tb_frame`, `f_back`, `f_globals`, `f_locals` and `f_builtins`.
2. Replace `getattr` in the sandbox builtins with a version that applies the
   same check.
3. Wrap every imported module in a `_ModuleProxy`. When an attribute is itself
   a module, the proxy returns it only if it is in `_SAFE_IMPORTS`. Otherwise
   it raises. This stops `re.enum.sys` and `PIL.Image.os`.
4. Remove more builtins: `delattr`, `dir`, `globals`, `help`, `locals`,
   `memoryview`, `setattr` and `vars`.

Also block the PIL file names `open`, `save`, `load`, `load_path`, `show` and
`truetype`. In `RecordingPyAutoGUI`, reject `screenshot()` if it gets a file
name. Without these checks, `Image.open(path)` reads files and `img.save(path)`
writes them.

Normal code still works: `json`, `math`, `from PIL import Image`, classes,
`time.sleep` and all recorded pyautogui calls.

**Test.** `test_sandbox_blocks_known_escapes` in the patch runs each escape
and checks for `SandboxViolationError`.

**Limit.** This is defense in depth, not isolation (AGENTS.md invariant 11).
Real isolation needs a separate process or VM. Plan that as its own feature.

## 2. Any website can fake verifier results

**Problem.** `demo/app.py` accepts `POST /api/event` and `POST /api/reset`
from any origin. It parses the body as JSON whatever the `Content-Type`
header says. So any page open in the operator's browser can send this:

```js
fetch("http://127.0.0.1:8765/api/event", {method: "POST", mode: "no-cors",
  body: '{"type":"submit","success":true}'})
```

The verifier then reports success. The evaluator accepts or rejects policies
from these numbers, so the attacker controls policy evolution.

A bad `Content-Length` header also breaks the handler. A negative value blocks
the read, and invalid JSON crashes it.

**Fix.** Add a `_same_origin()` check at the top of `do_POST`:

- `Host` must be `127.0.0.1:8765` or `localhost:8765`. This stops DNS rebinding.
- `Origin` must be missing or `http://` plus one of those hosts. Browsers
  always send `Origin` on a cross-site POST. `curl` and `Invoke-RestMethod`
  send none, so the README reset command still works.

Return 403 when the check fails. Wrap the length and JSON parse in `try`, and
return 400 on bad input.

**Test.** Run `demo/app.py`, then:

| Request | Expected |
|---|---|
| `Origin: https://evil.example` | 403 |
| `Host: evil.example:8765` | 403 |
| `Origin: http://127.0.0.1:8765` | 200 |
| `curl -X POST .../api/reset` | 200 |
| body `nope` | 400 |

## 3. Some pyautogui calls skip policy and recording

**Problem.** `RecordingPyAutoGUI` records and checks only the functions in
`_RECORDED_ACTIONS`. It passes every other name straight through. These still
move the mouse or type, but skip the tool allowlist, the action budget, the
screenshot cadence and telemetry: `mouseDown`, `mouseUp`, `keyDown`, `keyUp`,
`hold`, `drag`, `dragRel`, `move`, `moveRel`, `tripleClick`, `leftClick`,
`middleClick`, `hscroll`, `vscroll` and `run`.

**Fix.** Add these names to an `_UNRECORDED_ACTIONS` set. In `__getattr__`,
raise `PolicyViolationError` for them before anything else. The recorded
calls cover the same needs: `dragTo`, `moveTo`, `hotkey` and `scroll`.

To allow one of them later, follow "If you add a new pyautogui action" in
AGENTS.md.

**Test.** `test_sandbox_blocks_unrecorded_and_file_actions` in the patch.

## 4. Dashboard: unescaped HTML and DNS rebinding

**Problem.** `dashboard/app.py` escapes most values, but not these:

- `policy_version` and the three counts in `metric_cards()`
- `previous.get('version')` and `newest.get('version')` in the policy diff
- `evaluation['decision']` inside a `class='...'` attribute

A document in Atlas with `"decision": "x' onmouseover='..."` runs script in
the dashboard.

The dashboard also serves Atlas data to any `Host` header. A DNS-rebinding
page can read runs, lessons and policies.

**Fix.** Add a helper and use it on every value from MongoDB:

```python
def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)
```

At the top of `do_GET`, return 403 unless `Host` is `127.0.0.1:8787` or
`localhost:8787`.

## 5. Atlas lessons go into the system prompt unchecked

**Problem.** `_propose_candidate` copies every `lesson` it gets back from
vector search into the new policy's rules. `agent.py` then writes the rules
into the system prompt as "mandatory". Anyone who can write to the
`experiences` collection can give the agent new instructions. A stored rule
can also contain newlines and pose as a new prompt section.

**Fix.**

- `evolution/runtime.py`: move the four lesson strings from `_lesson()` into
  a `LESSONS` dict. In `_propose_candidate`, keep only retrieved lessons that
  are in `LESSONS.values()`. The harness writes only these strings, so
  nothing legitimate is lost.
- `agent.py`: fold each rule to one line and cap it at 300 characters before
  it goes into the prompt:

```python
policy_text = "\n".join(f"- {' '.join(str(rule).split())[:300]}" for rule in policy.rules)
```

Also finish the open item in `goals/GOALS.md`: give the dashboard a read-only
database user. The same idea applies to the agent. Keep the set of users who
can write to `experiences` and `policies` small.

## 6. `test_store` fails on `main`

**Problem.** `store.finish_run` writes `final_summary`, which is the redacted
and bounded text. The test still expects the old field `final_text`.

**Fix.** In `tests/test_store.py`, change the assertion to
`finish["final_summary"]`. The store is right and the test is stale.

## Already fine

- `.env` is in `.gitignore`, and git history contains no keys or Atlas
  passwords.
- `store._redact_uri` hides Mongo credentials in logs.
- `verification.py` accepts only `http://` URLs on localhost.
