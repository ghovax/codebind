# Codebind

Codebind runs a model with one tool: Python executed in the IPython session shared with the user. Conversation history belongs to a `Session`; the model is selected independently for every call.

## Installation

Run Codebind without installing it permanently:

```console
uvx codebind
```

Or install it with any Python package installer:

```console
pip install codebind
```

After installation, start the preconfigured shell from any directory:

```console
codebind
```

It opens IPython with `chat` and `Models` already available and renders a short Markdown usage guide in the terminal. It does not modify the user's global IPython profile.

```python
models = Models({"openai": "OPENAI_API_KEY"})

chat.ask(
    "Inspect this project and tell me what to implement first.",
    models.chat("openai/gpt-5", reasoning_effort="medium"),
)
```

Codebind does not load files or construct a project prompt automatically. The user states what should be loaded as context. Pass `instructions=` to `Session` only when an application needs its own system instructions.

## ChatGPT account login

Models Provider can start its OpenAI browser sign-in flow directly from IPython:

```python
import webbrowser

models = Models()
authorization = await models.sign_in("openai")
webbrowser.open(authorization.url)
await authorization.complete()

chat.ask(
    "Inspect this project.",
    models.chat("openai/gpt-5", authorization=authorization),
)
```

The authorization remains in memory for this session. Persistent credential storage belongs to the host application.

OpenAI officially supports ChatGPT subscription sign-in for Codex clients. Models Provider reproduces that account-access boundary for this library; it is separate from the public, pay-as-you-go OpenAI API and may require compatibility updates when the Codex account protocol changes.
