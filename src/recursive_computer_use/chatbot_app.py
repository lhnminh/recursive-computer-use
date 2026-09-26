"""Local Streamlit chat surface for the recursive computer-use harness."""

from __future__ import annotations

import os
import threading

import streamlit as st
from dotenv import load_dotenv

from recursive_computer_use.chat_runtime import (
    ChatOptions,
    DEFAULT_MODEL,
    DEFAULT_TASK_KEY,
    execute_task,
    safe_error,
)


load_dotenv()

st.set_page_config(
    page_title="Recursive Computer Use",
    page_icon="🖥️",
    layout="centered",
)


@st.cache_resource
def _desktop_lock() -> threading.Lock:
    """One process-wide lock prevents concurrent control of one desktop."""

    return threading.Lock()


def _initial_messages() -> list[dict[str, str]]:
    return [
        {
            "role": "assistant",
            "content": (
                "Tell me what to do on this computer. I will inspect the screen, "
                "operate the local mouse and keyboard, and report the result."
            ),
        }
    ]


if "messages" not in st.session_state:
    st.session_state.messages = _initial_messages()

st.title("Recursive Computer Use")
st.caption(
    "A local chat interface for the policy-enforced desktop agent. "
    "Submitting a task can move your mouse and type on your keyboard."
)

with st.sidebar:
    st.header("Control")
    armed = st.toggle(
        "Arm computer control",
        value=False,
        help="Required before a chat message can start desktop automation.",
    )
    stop_before_irreversible = st.toggle(
        "Stop before irreversible actions",
        value=True,
        help="Prepare work but stop before sending, publishing, buying, deleting, or confirming.",
    )

    st.header("Harness")
    model = st.text_input("Model", value=os.environ.get("RCU_MODEL", DEFAULT_MODEL))
    task_key = st.text_input(
        "Task family",
        value=DEFAULT_TASK_KEY,
        help="Related runs share retrieved experience and policy versions.",
    )
    verifier_url = st.text_input(
        "Local verifier URL",
        value="",
        placeholder="http://127.0.0.1:8765/api/result",
        help="Optional. Only a localhost verifier can approve self-improvement.",
    )

    mongo_configured = bool(os.environ.get("MONGODB_URI", "").strip())
    log_actions = st.toggle(
        "MongoDB telemetry",
        value=mongo_configured,
        help="Stores sanitized actions and bounded redacted summaries.",
    )
    evolve = st.toggle(
        "Self-improvement",
        value=mongo_configured,
        disabled=not log_actions,
        help="Requires MongoDB. Policy promotion still requires a deterministic verifier.",
    )
    verbose = st.toggle("Verbose terminal logs", value=False)

    if mongo_configured:
        st.success("MongoDB configured through .env", icon="✅")
    else:
        st.info("MongoDB is not configured. Tasks can still run without learning.")
    if evolve and not verifier_url.strip():
        st.warning("Without a verifier, runs remain unverified and cannot promote a policy.")

    if st.button("Clear chat", use_container_width=True):
        st.session_state.messages = _initial_messages()
        st.rerun()

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

prompt = st.chat_input(
    "Tell the agent what to do on this computer",
    disabled=not armed,
)

if not armed:
    st.info("Arm computer control in the sidebar to submit a task.")

if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        lock = _desktop_lock()
        if not lock.acquire(blocking=False):
            answer = "Another task is controlling this desktop. Wait for it to finish and try again."
            st.warning(answer)
        else:
            try:
                options = ChatOptions(
                    model=model,
                    task_key=task_key,
                    verifier_url=verifier_url or None,
                    mongodb_db=os.environ.get("MONGODB_DB") or None,
                    log_actions=log_actions,
                    evolve=evolve,
                    verbose=verbose,
                    stop_before_irreversible=stop_before_irreversible,
                )
                with st.status("Agent is controlling the local desktop", expanded=True) as status:
                    st.write("Keep the target window visible. Move the pointer to a screen corner to trigger the pyautogui fail-safe.")
                    result = execute_task(prompt, options)
                    status.update(label="Task finished", state="complete", expanded=False)
                answer = result.strip() or "The agent finished without a text response."
                st.markdown(answer)
            except KeyboardInterrupt:
                answer = "Task interrupted."
                st.warning(answer)
            except Exception as exc:  # Streamlit must survive a failed desktop run.
                answer = f"Task failed: {safe_error(exc)}"
                st.error(answer)
            finally:
                lock.release()

        st.session_state.messages.append({"role": "assistant", "content": answer})

st.caption(
    "Chat messages stay in this Streamlit session. Existing telemetry rules store "
    "only sanitized actions and bounded redacted summaries."
)
