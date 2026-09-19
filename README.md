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

chat.send(
    "Inspect this project and tell me what to implement first.",
    models.chat("openai/gpt-5", reasoning_effort="medium"),
)
```

Codebind does not load files or construct a project prompt automatically. The user states what should be loaded as context. Pass `instructions=` to `Session` only when an application needs its own system instructions.

## Invocations

`send()` and `asend()` run one visible agent invocation, including every repeated model and IPython step required to reach a final response. `invoke()` starts an independent invocation with its own history and returns an awaitable handle:

```python
import asyncio

first = chat.invoke("Review the API.", model)
second = chat.invoke("Review the packaging.", model)

first_outcome, second_outcome = await asyncio.gather(first, second)
```

Fork explicitly from an immutable history snapshot:

```python
branch = chat.invoke(
    "Try a different solution.",
    model,
    history=first.history,
)
outcome = await branch
```

Each invocation has an `id`, `parent_id`, `history`, `status`, and one terminal `outcome`. Waiting, parallelism, and joining results remain ordinary Python operations.

## Jupyter

Start JupyterLab with Codebind from any directory without a permanent installation:

```console
uvx --from jupyterlab --with codebind jupyter-lab
```

Or install both packages into the same environment, then start JupyterLab normally:

```console
pip install codebind jupyterlab
jupyter lab
```

Load Codebind in a notebook:

```python
%load_ext codebind
```

Run that cell once so JupyterLab can connect to the extension, then use Codebind in later cells:

```python
models = Models({"openai": "OPENAI_API_KEY"})
model = models.chat("openai/gpt-5")
await chat.asend("Inspect the current notebook state.", model)
```

The Codebind package includes a prebuilt JupyterLab extension. In JupyterLab, each model-authored IPython execution becomes a genuine code cell with its native execution count and outputs, and the assistant response becomes a rendered Markdown cell. The cells are ordinary notebook content and are saved with the notebook.

Other IPython frontends use the standard MIME display protocol instead. They still receive syntax-highlighted code, assistant Markdown, stdout, tracebacks, rich results, and native IPython history without Codebind depending on their UI.

## ChatGPT account login

Models Provider can start its OpenAI browser sign-in flow directly from IPython:

```python
import webbrowser

models = Models()
authorization = await models.sign_in("openai")
webbrowser.open(authorization.url)
await authorization.complete()

chat.send(
    "Inspect this project.",
    models.chat("openai/gpt-5", authorization=authorization),
)
```

The authorization remains in memory for this session. Persistent credential storage belongs to the host application.

OpenAI officially supports ChatGPT subscription sign-in for Codex clients. Models Provider reproduces that account-access boundary for this library; it is separate from the public, pay-as-you-go OpenAI API and may require compatibility updates when the Codex account protocol changes.
