# Codebind

Codebind turns an ordinary IPython session into a durable model harness with one tool: an IPython cell executed in the active session. IPython owns execution, namespace, history, magics, tracebacks, and rich display; Codebind owns conversation state and model orchestration.

## Installation

```console
uvx codebind
```

Or install it with any Python package installer:

```console
pip install codebind
```

## Configuration

Codebind reads its private model choice from `$XDG_CONFIG_HOME/codebind/configuration.json`, or `~/.config/codebind/configuration.json` when `XDG_CONFIG_HOME` is unset:

```json
{
  "model": "openai/gpt-5.6-luna",
  "parameters": {
    "reasoning_effort": "medium"
  }
}
```

Provider credentials use the same XDG directory in `models.json`. Keep both files readable only by their owner. Loading Codebind does not add `chat`, `model`, `models`, `Models`, or any other variable to the IPython namespace.

## Terminal IPython

Start a new conversation:

```console
uvx codebind
```

Ask through ordinary IPython magic syntax:

```python
%%question
Inspect this project and tell me what to implement first.
```

Terminal conversations remain live for the current IPython process. Durable automatic resumption belongs to notebooks, where the notebook file provides an unambiguous conversation identity.

## JupyterLab

```console
uvx --from jupyterlab --with codebind --with 'nbconvert[webpdf]' jupyter lab
```

When running an unpublished local checkout, expose both its editable Python source and its current prebuilt frontend:

```console
JUPYTER_PATH=/path/to/codebind/data/share/jupyter \
uvx --refresh --from jupyterlab --with-editable /path/to/codebind \
  --with 'nbconvert[webpdf]' jupyter-lab
```

Load Codebind in a notebook:

```python
%load_ext codebind
```

That is the complete setup. **Question** is a native toolbar toggle. Turning it on converts the selected cell into a Question and automatically makes each newly created user cell a Question until the toggle is turned off. Codebind-generated instruction, tool, and answer cells are never converted. Write ordinary Markdown and press `Shift+Enter`.

Loading the extension adds a locked Markdown cell containing Codebind's packaged instructions. That exact text becomes the conversation's immutable system message and remains stable when the server or kernel restarts.

The whole notebook is model context. Before each Question, Codebind takes a canonical snapshot of ordinary Markdown, raw, and code cells, including compact visible text outputs but never the live Python namespace. The first turn records the snapshot; later turns append only new, changed, removed, or reordered cells. Questions, tool calls, tool results, and answers already present in the conversation ledger are not duplicated. This append-only representation keeps the prior model prefix unchanged for provider caching.

Codebind stores the complete LangChain message, notebook-context, and turn ledger in notebook metadata. Reopening the notebook, restarting its kernel, and loading the extension restores the exact accumulated context automatically. Assistant Markdown streams into its native cell as it is generated, before a following tool call is complete. Tool executions and assistant answers are always appended to the notebook end without changing the user's current selection or scroll position. Tool outputs are collapsed by default and remain expandable through JupyterLab's native output control.

An interrupted tool call is closed with an explicit interrupted result before the turn ends. If a provider stream is cancelled or fails, Codebind discards that provider session before the next Question while retaining the notebook conversation and its stable prompt-cache identity.

The instruction cell, sent Question cells, model-authored tool cells, and assistant Markdown cells are non-editable and non-deletable. Draft Question cells remain editable until they are sent. Jupyter's standard interrupt button cancels an active Codebind question.

Question and assistant Markdown supports `$...$`, `$$...$$`, `\(...\)`, and `\[...\]` through JupyterLab's native MathJax renderer.

Other IPython frontends retain standard MIME display, syntax-highlighted code, Markdown, stdout, tracebacks, rich results, and native IPython history.

## ChatGPT account access

Models Provider owns OpenAI account authorization and token refresh. Save the resulting provider-values object in Codebind's XDG `models.json`; Codebind loads it privately when the extension starts. ChatGPT subscription access is separate from the public, pay-as-you-go OpenAI API and may require compatibility updates when the account protocol changes.
