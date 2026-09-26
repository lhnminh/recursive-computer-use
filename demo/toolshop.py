"""Toolshop adapter for the live demo: a real public shop built for automation practice.

Site: https://practicesoftwaretesting.com (UI) backed by
https://api.practicesoftwaretesting.com (JSON API). Anonymous carts only; we
never log in or place an order, and requests stay low (a few per task).

Task: "Add the cheapest <item> to the cart."
Grader: read the cart back through the site's API. 1.0 if it holds the
cheapest matching product, 0.5 if it holds a matching but pricier one, 0
otherwise. Neither agent grades itself.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from recursive_computer_use.network.learner import (
    DEFAULT_MODEL,
    _complete,
    _parse_json,
    fill_params,
    learn_recipe,
    llm_chooser,
)
from recursive_computer_use.network.recipe import Recipe
from recursive_computer_use.network.runner import run_recipe
from scripts import webshop_eval as ws

UI = "https://practicesoftwaretesting.com"
API = "https://api.practicesoftwaretesting.com"
SITE = "api.practicesoftwaretesting.com"
TASK_KEY = "toolshop"
ITEMS = ["pliers", "hammer", "screwdriver", "saw", "wrench", "chisel", "tape", "drill"]
TEACH_ITEM = "pliers"
HAR = Path(".recordings/toolshop_demo.har")
MAX_STEPS = 12
Event = Callable[[dict[str, Any]], None]
_emit = ws._emit


def instruction(item: str) -> str:
    return f"Add the cheapest {item} to the cart."


def tasks() -> dict[str, str]:
    return {item: instruction(item) for item in ITEMS}


def _get(path: str) -> Any:
    with urlopen(Request(API + path, headers={"accept": "application/json"}), timeout=20) as r:
        return json.loads(r.read())


def cheapest(item: str) -> tuple[set[str], float | None]:
    """Ids of the cheapest products whose name matches *item*, from the site's own search."""
    matches: list[dict[str, Any]] = []
    page = 1
    while page <= 5:
        d = _get(f"/products/search?q={item}&page={page}")
        matches += [p for p in d.get("data", []) if item in p.get("name", "").lower()]
        if page >= d.get("last_page", 1):
            break
        page += 1
    if not matches:
        return set(), None
    low = min(float(p["price"]) for p in matches)
    return {p["id"] for p in matches if float(p["price"]) == low}, low


def grade(cart_id: str | None, item: str) -> tuple[float, str]:
    if not cart_id:
        return 0.0, "no cart"
    try:
        cart = _get(f"/carts/{cart_id}")
    except Exception as exc:  # noqa: BLE001
        return 0.0, f"cart unreadable ({type(exc).__name__})"
    products = [ci.get("product") or {} for ci in cart.get("cart_items", [])]
    if not products:
        return 0.0, "empty cart"
    best, low = cheapest(item)
    names = ", ".join(f"{p.get('name')} ${p.get('price')}" for p in products)
    if any(p.get("id") in best for p in products):
        return 1.0, f"cart: {names} (cheapest is ${low})"
    if any(item in (p.get("name") or "").lower() for p in products):
        return 0.5, f"cart: {names} (cheapest is ${low})"
    return 0.0, f"cart: {names}"


# -- teach -----------------------------------------------------------------------


def teach(store: Any, on_event: Event, *, show: bool, window: tuple[int, int, int, int] | None) -> Recipe:
    """A person-like demo on the real site (search, pick the cheapest, add to cart), recorded."""
    from playwright.sync_api import sync_playwright

    HAR.parent.mkdir(exist_ok=True)
    task = instruction(TEACH_ITEM)
    _emit(on_event, "task", task)
    _emit(on_event, "record", f"Recording one run on {UI} (network traffic -> HAR, stays on this Mac)")
    with sync_playwright() as p:
        args = [f"--window-position={window[0]},{window[1]}", f"--window-size={window[2]},{window[3]}"] if window else []
        browser = p.chromium.launch(headless=not show, slow_mo=300 if show else 0, args=args)
        ctx = browser.new_context(record_har_path=str(HAR), record_har_content="embed", no_viewport=bool(window))
        page = ctx.new_page()
        page.goto(UI, wait_until="networkidle")
        page.fill("[data-test=search-query]", TEACH_ITEM)
        page.click("[data-test=search-submit]")
        page.wait_for_load_state("networkidle")
        cards = page.locator("a.card")
        prices = [float(re.sub(r"[^0-9.]", "", cards.nth(i).locator("[data-test=product-price]").inner_text()) or 1e9)
                  for i in range(cards.count())]
        cards.nth(prices.index(min(prices))).click()  # a person reads the prices and opens the cheapest
        page.wait_for_load_state("networkidle")
        page.click("[data-test=add-to-cart]")
        page.wait_for_timeout(2500)
        cart_id = page.evaluate("() => sessionStorage.getItem('cart_id')")
        ctx.close()
        browser.close()
    score, detail = grade(cart_id, TEACH_ITEM)
    _emit(on_event, "record", f"Demo run graded {score:.2f}: {detail}")
    _emit(on_event, "learn", "Model reads the redacted traffic and writes an API recipe...")
    t = time.time()
    recipe = learn_recipe(HAR, task, site=SITE, task_key=TASK_KEY)
    _emit(on_event, "learn", f"Recipe learned in {time.time() - t:.1f}s")
    for step in recipe.steps:
        chooses = ", ".join(f"choose {c.var}" for c in step.choose)
        _emit(on_event, "step", f"{step.method} {step.url.split(SITE, 1)[-1]}" + (f"  ({chooses})" if chooses else ""))
    recipe.id = store.save_candidate(recipe)
    _emit(on_event, "atlas", f"Saved to MongoDB as {recipe.name} v{recipe.version} ({recipe.status})")
    return recipe


# -- recipe agent ----------------------------------------------------------------


def _cart_var(recipe: Recipe) -> str | None:
    for step in recipe.steps:
        if step.method == "POST" and re.search(r"/carts/?$", step.url):
            for ex in step.extract:
                return ex.var
    return None


def run_recipe_task(recipe: Recipe, item: str, client: Any, on_event: Event | None = None) -> dict[str, Any]:
    emit = on_event or (lambda _e: None)
    stats = {"llm_calls": 0}
    t = time.time()
    task = instruction(item)
    base = llm_chooser(client=client, stats=stats)

    def chooser(task_: str, ch: Any, cands: list[dict[str, Any]]) -> Any:
        pick = base(task_, ch, cands)
        if ch.mode == "one" and isinstance(pick, int) and 0 <= pick < len(cands):
            _emit(emit, "choose", f"Picked {cands[pick]['context'][:140]}")
        return pick

    params: dict[str, str] = {}
    result = None
    error = None
    try:
        _emit(emit, "task", task)
        params = fill_params(recipe, task, client=client)
        stats["llm_calls"] += 1
        _emit(emit, "params", "Params: " + ", ".join(f"{k}={v!r}" for k, v in params.items()))
        result = run_recipe(recipe, params, session_cookies={}, chooser=chooser, task=task,
                            on_step=lambda s, _c: _emit(emit, "http", f"{s.id}: HTTP {s.status} in {s.ms} ms"))
        error = result.error
    except Exception as exc:  # noqa: BLE001
        error = repr(exc)[:200]
    cart_id = result.vars.get(_cart_var(recipe) or "") if result else None
    score, detail = grade(cart_id, item)
    row = {"arm": "recipe", "session": item, "reward": score, "seconds": time.time() - t,
           "llm_calls": stats["llm_calls"], "error": error, "cart_id": cart_id, "detail": detail,
           "chosen": {k: v for k, v in (result.vars.items() if result else []) if k in _choose_vars(recipe)}}
    _emit(emit, "done", f"Score {score:.2f} in {row['seconds']:.1f}s, {row['llm_calls']} model calls. {detail}"
          + (f" ({error})" if error else ""), row=row)
    return row


def _choose_vars(recipe: Recipe) -> set[str]:
    return {c.var for s in recipe.steps for c in s.choose}


def run_from_atlas(store: Any, item: str, client: Any, on_event: Event | None = None) -> dict[str, Any]:
    emit = on_event or (lambda _e: None)
    found = store.find_for_task(instruction(item), site=SITE, limit=1)
    if not found:
        _emit(emit, "atlas", "No Toolshop recipe in MongoDB yet. Teach one first.")
        row = {"arm": "recipe", "session": item, "reward": 0.0, "seconds": 0.0, "llm_calls": 0,
               "error": "no recipe in Atlas"}
        _emit(emit, "done", "No recipe", row=row)
        return row
    recipe = found[0]
    doc = store.skills.find_one({"_id": recipe.id}, {"uses": 1, "wins": 1}) or {}
    _emit(emit, "atlas", f"Found in MongoDB by meaning: {recipe.name} v{recipe.version} ({recipe.status}), "
          f"used {doc.get('uses', 0)}x, won {doc.get('wins', 0)}x")
    row = run_recipe_task(recipe, item, client, emit)
    status = store.record_result(recipe.id, ok=row["reward"] >= ws.RECIPE_OK_REWARD,
                                 run_ms=int(row["seconds"] * 1000), steps=[{}] * len(recipe.steps),
                                 task=instruction(item), llm_calls=row["llm_calls"])
    _emit(emit, "atlas", f"Result written to MongoDB; recipe is now {status}")
    row["recipe"] = recipe
    return row


def show_cart(page: Any, row: dict[str, Any], banner: Callable[..., None]) -> None:
    """Open the recipe agent's real cart on the site (the cart id is the site's own)."""
    if not row.get("cart_id"):
        return
    label = f"Agent using MongoDB · done via API in {row['seconds']:.1f}s · {row['llm_calls']} model calls"
    page.goto(UI, wait_until="domcontentloaded")
    page.evaluate("id => { sessionStorage.setItem('cart_id', id); sessionStorage.setItem('cart_quantity', '1'); }",
                  row["cart_id"])
    product = next(iter((row.get("chosen") or {}).values()), None)
    if product:
        page.goto(f"{UI}/product/{product}", wait_until="networkidle")
        banner(page, label, "#1f6feb")
        time.sleep(1.5)
    page.goto(f"{UI}/checkout", wait_until="networkidle")
    banner(page, label + f" · score {row['reward']:.2f}", "#238636" if row["reward"] >= 0.99 else "#9e6a03")


# -- browsing agent ----------------------------------------------------------------

BROWSE_SYSTEM = """You shop on an online hardware store to complete an instruction.
Each turn you see the page text and numbered actions. Reply with one JSON
object: {"action": "search", "query": "..."} or {"action": "click", "n": <number>}
or {"action": "done"} once the right item is in the cart. Be efficient."""


def _settle(page: Any) -> None:
    """The site renders with JavaScript after load; wait for the elements an agent needs."""
    target = "[data-test=add-to-cart]" if "/product/" in page.url else "a.card, [data-test=search-query]"
    try:
        page.wait_for_selector(target, timeout=6000)
    except Exception:  # noqa: BLE001 - some pages have neither
        pass


def _actions(page: Any) -> list[dict[str, Any]]:
    _settle(page)
    acts: list[dict[str, Any]] = []
    on_product = page.locator("[data-test=add-to-cart]").count() > 0
    if on_product:
        name, price = "this product", ""
        try:  # the page can re-render under us; never hang on a label
            name = page.locator("[data-test=product-name]").first.inner_text(timeout=3000)
            price = page.locator("[data-test=unit-price]").first.inner_text(timeout=3000)
        except Exception:  # noqa: BLE001
            pass
        acts.append({"label": f"Add this product to cart: {name} ${price}".strip(), "loc": ("[data-test=add-to-cart]", 0)})
    if not on_product and page.locator("[data-test=sort]").count():
        acts.append({"label": "sort results: Price (Low - High)", "loc": ("sort", "price,asc")})
    cards = page.locator("a.card")
    kind = "open related product" if on_product else "open product"
    for i in range(min(cards.count(), 12)):
        try:
            text = re.sub(r"\s+", " ", cards.nth(i).inner_text(timeout=3000)).strip()
        except Exception:  # noqa: BLE001
            continue
        text = re.sub(r"\b[A-E](?: [A-E]){4}\b", "", text).replace("More information", "").strip()
        acts.append({"label": f"{kind}: {text[:80]}", "loc": ("a.card", i)})
    pages = page.locator("ul.pagination a.page-link")
    for i in range(min(pages.count(), 6)):
        acts.append({"label": f"results page {pages.nth(i).inner_text().strip()}", "loc": ("ul.pagination a.page-link", i)})
    if "/product/" in page.url:
        acts.append({"label": "go back", "loc": None})
    return acts


def browse_visible(page: Any, item: str, client: Any, on_event: Event | None,
                   banner: Callable[..., None]) -> dict[str, Any]:
    emit = on_event or (lambda _e: None)
    t = time.time()
    task = instruction(item)
    history: list[str] = []
    calls, error = 0, None
    _emit(emit, "task", task)
    try:
        page.goto(UI, wait_until="networkidle")
        for _ in range(MAX_STEPS):
            banner(page, f"Browsing agent · no memory · {calls} model calls · {time.time() - t:.0f}s", "#6e40c9")
            acts = _actions(page)
            prompt = {"instruction": task, "history": history[-6:],
                      "page": re.sub(r"\s+", " ", page.inner_text("body", timeout=5000))[:3500],
                      "actions": "\n".join(f"{i}: {a['label']}" for i, a in enumerate(acts)),
                      "can_search": page.locator("[data-test=search-query]").count() > 0}
            reply = _complete(client, DEFAULT_MODEL, [
                {"role": "system", "content": BROWSE_SYSTEM},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ])
            calls += 1
            try:
                act = _parse_json(reply)
            except ValueError:
                act = {"action": "done"}
            kind = act.get("action")
            if kind == "done":
                _emit(emit, "action", "done")
                break
            if kind == "search":
                q = str(act.get("query", ""))[:80]
                history.append(f"search[{q}]")
                _emit(emit, "action", f"search[{q}]")
                if not page.locator("[data-test=search-query]").count():
                    page.goto(UI, wait_until="networkidle")
                page.fill("[data-test=search-query]", q)
                page.click("[data-test=search-submit]")
                page.wait_for_load_state("networkidle")
                continue
            n = int(act.get("n", -1)) if str(act.get("n", "")).lstrip("-").isdigit() else -1
            if not 0 <= n < len(acts):
                history.append("invalid action")
                continue
            a = acts[n]
            history.append(f"click[{a['label']}]")
            _emit(emit, "action", f"click[{a['label']}]")
            try:
                if a["loc"] is None:
                    page.go_back(wait_until="networkidle")
                elif a["loc"][0] == "sort":
                    page.select_option("[data-test=sort]", a["loc"][1])
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(800)
                else:
                    page.locator(a["loc"][0]).nth(a["loc"][1]).click(timeout=8000)
                    page.wait_for_load_state("networkidle")
            except Exception:  # noqa: BLE001 - a failed click is a wasted turn, not a crash
                history.append("that click failed")
                continue
                if a["label"].startswith("Add this product to cart"):
                    page.wait_for_timeout(1500)
        else:
            error = "step limit"
    except Exception as exc:  # noqa: BLE001
        error = repr(exc)[:200]
    cart_id = None
    try:
        cart_id = page.evaluate("() => sessionStorage.getItem('cart_id')")
    except Exception:  # noqa: BLE001
        pass
    score, detail = grade(cart_id, item)
    row = {"arm": "baseline", "session": item, "reward": score, "seconds": time.time() - t,
           "llm_calls": calls, "error": error, "detail": detail, "trace": history}
    banner(page, f"Browsing agent · score {score:.2f} · {row['seconds']:.1f}s · {calls} model calls",
           "#238636" if score >= 0.99 else "#da3633")
    _emit(emit, "done", f"Score {score:.2f} in {row['seconds']:.1f}s, {calls} model calls. {detail}"
          + (f" ({error})" if error else ""), row=row)
    return row
