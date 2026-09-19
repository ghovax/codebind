# Codebind

Codebind is an IPython extension that runs a model with one tool: an IPython cell executed in the active session. IPython owns execution, namespace, history, magics, tracebacks, and rich display; Codebind owns only conversation and model orchestration.

## Installation

Run Codebind without installing it permanently:

```console
uvx codebind
```

Or install it with any Python package installer:

```console
pip install codebind
```

After installation, start ordinary IPython with the Codebind extension from any directory:

```console
codebind
```

It opens standard IPython with `chat` and `Models` in the user namespace. All normal IPython command-line options remain available.

```python
models = Models({"openai": "OPENAI_API_KEY"})

chat.ask(
    "Inspect this project and tell me what to implement first.",
    models.chat("openai/gpt-5", reasoning_effort="medium"),
)
```

Codebind does not load files or construct a project prompt automatically. The user states what should be loaded as context. Pass `instructions=` to `Session` only when an application needs its own system instructions.

## Jupyter

Install Codebind in the environment used by a Jupyter kernel, then start the Jupyter frontend normally:

```console
pip install codebind jupyterlab
jupyter lab
```

Load Codebind in a notebook:

```python
%load_ext codebind

models = Models({"openai": "OPENAI_API_KEY"})
model = models.chat("openai/gpt-5")
await chat.aask("Inspect the current notebook state.", model)
```

Codebind publishes cells, assistant Markdown, stdout, tracebacks, and rich results through IPython's MIME display system. The active frontend decides how to render HTML, Markdown, images, SVG, audio, tables, and plain text.

Model-authored cells are recorded in native IPython history and displayed through the active frontend. A kernel cannot insert a genuine input cell into every possible frontend without a frontend-specific extension, so Codebind does not attempt to control notebook or editor UI.

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
