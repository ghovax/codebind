import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import {
  CodeCell,
  ICellModel,
  ICodeCellModel,
  MarkdownCell
} from '@jupyterlab/cells';
import * as nbformat from '@jupyterlab/nbformat';
import {
  INotebookTracker,
  NotebookActions,
  NotebookPanel
} from '@jupyterlab/notebook';
import { Kernel, KernelMessage } from '@jupyterlab/services';

const TARGET_NAME = 'codebind';

interface CodeCellMessage {
  type: 'code_cell';
  source: string;
  execution_count: number | null;
  outputs: nbformat.IOutput[];
}

interface MarkdownCellMessage {
  type: 'markdown_cell';
  source: string;
}

type CodebindMessage = CodeCellMessage | MarkdownCellMessage;

interface InvocationStartedMessage {
  type: 'invocation_started';
  invocation_id: string;
}

interface ExecuteCellMessage {
  type: 'execute_cell';
  invocation_id: string;
  request_id: string;
  source: string;
}

interface InvocationCompletedMessage {
  type: 'invocation_completed';
  invocation_id: string;
  source: string;
}

type InvocationMessage =
  | InvocationStartedMessage
  | ExecuteCellMessage
  | InvocationCompletedMessage;

interface TurnState {
  parentModel: ICodeCellModel | null;
  parentExecutionCount: number | null;
  resumeModel: ICellModel | null;
  nextIndex: number;
}

interface InvocationState {
  id: string;
  anchorModel: ICellModel;
  lastModel: ICellModel;
}

function isCodebindMessage(value: unknown): value is CodebindMessage {
  if (typeof value !== 'object' || value === null) {
    return false;
  }
  const message = value as Record<string, unknown>;
  if (message.type === 'markdown_cell') {
    return typeof message.source === 'string';
  }
  return (
    message.type === 'code_cell' &&
    typeof message.source === 'string' &&
    (typeof message.execution_count === 'number' ||
      message.execution_count === null) &&
    Array.isArray(message.outputs)
  );
}

function isInvocationMessage(value: unknown): value is InvocationMessage {
  if (typeof value !== 'object' || value === null) {
    return false;
  }
  const message = value as Record<string, unknown>;
  if (
    message.type === 'invocation_started' &&
    typeof message.invocation_id === 'string'
  ) {
    return true;
  }
  if (
    message.type === 'invocation_completed' &&
    typeof message.invocation_id === 'string' &&
    typeof message.source === 'string'
  ) {
    return true;
  }
  return (
    message.type === 'execute_cell' &&
    typeof message.invocation_id === 'string' &&
    typeof message.request_id === 'string' &&
    typeof message.source === 'string'
  );
}

function insertCell(
  panel: NotebookPanel,
  message: CodebindMessage,
  index: number
): void {
  const notebook = panel.content;
  const model = notebook.model;
  if (!model) {
    return;
  }

  if (message.type === 'code_cell') {
    model.sharedModel.insertCell(index, {
      cell_type: 'code',
      source: message.source,
      metadata: { trusted: true },
      execution_count: message.execution_count,
      outputs: message.outputs
    });
  } else {
    model.sharedModel.insertCell(index, {
      cell_type: 'markdown',
      source: message.source,
      metadata: {}
    });
  }

  notebook.activeCellIndex = index;
  notebook.deselectAll();
  if (message.type === 'markdown_cell') {
    void NotebookActions.run(notebook);
  }
  void notebook.scrollToItem(index);
}

function runningCellIndex(panel: NotebookPanel): number {
  return panel.content.widgets.findIndex(
    widget =>
      widget.model.type === 'code' &&
      (widget.model as ICodeCellModel).executionState === 'running'
  );
}

function invocationInsertionIndex(
  panel: NotebookPanel,
  state: InvocationState,
  states: InvocationState[]
): number {
  const widgets = panel.content.widgets;
  let index = widgets.findIndex(widget => widget.model === state.lastModel) + 1;
  for (const previous of states) {
    if (previous === state) {
      break;
    }
    if (previous.anchorModel === state.anchorModel) {
      const previousIndex = widgets.findIndex(
        widget => widget.model === previous.lastModel
      );
      index = Math.max(index, previousIndex + 1);
    }
  }
  return Math.max(index, 0);
}

function startInvocation(
  panel: NotebookPanel,
  comm: Kernel.IComm,
  message: InvocationStartedMessage,
  invocations: Map<string, InvocationState>,
  states: InvocationState[]
): void {
  const notebook = panel.content;
  const parentIndex = runningCellIndex(panel);
  const anchorCell =
    (parentIndex >= 0 ? notebook.widgets[parentIndex] : null) ??
    notebook.activeCell;
  const anchor = anchorCell?.model;
  if (!anchor) {
    return;
  }
  const state = {
    id: message.invocation_id,
    anchorModel: anchor,
    lastModel: anchor
  };
  invocations.set(message.invocation_id, state);
  states.push(state);
  const completed =
    anchorCell instanceof CodeCell && anchorCell.outputArea.future
      ? anchorCell.outputArea.future.done
      : Promise.resolve();
  void completed.then(() => {
    comm.send({
      type: 'invocation_ready',
      invocation_id: message.invocation_id
    });
  });
}

async function executeInvocationCell(
  panel: NotebookPanel,
  comm: Kernel.IComm,
  message: ExecuteCellMessage,
  invocations: Map<string, InvocationState>,
  states: InvocationState[]
): Promise<void> {
  const state = invocations.get(message.invocation_id);
  const notebook = panel.content;
  const model = notebook.model;
  if (!state || !model) {
    comm.send({
      type: 'execution_result',
      request_id: message.request_id,
      error: 'invocation is not anchored to this notebook'
    });
    return;
  }

  const index = invocationInsertionIndex(panel, state, states);
  model.sharedModel.insertCell(index, {
    cell_type: 'code',
    source: message.source,
    metadata: { trusted: true },
    execution_count: null,
    outputs: []
  });
  await Promise.resolve();
  const cell = notebook.widgets[index];
  if (!(cell instanceof CodeCell)) {
    comm.send({
      type: 'execution_result',
      request_id: message.request_id,
      error: 'JupyterLab did not create a code cell'
    });
    return;
  }
  state.lastModel = cell.model;

  try {
    await CodeCell.execute(cell, panel.sessionContext, {
      codebind_invocation_id: message.invocation_id
    });
    comm.send({
      type: 'execution_result',
      request_id: message.request_id,
      execution_count: cell.model.executionCount,
      outputs: JSON.parse(JSON.stringify(cell.model.outputs.toJSON()))
    });
  } catch (error) {
    comm.send({
      type: 'execution_result',
      request_id: message.request_id,
      error: error instanceof Error ? error.message : String(error)
    });
  }
}

async function completeInvocation(
  panel: NotebookPanel,
  message: InvocationCompletedMessage,
  invocations: Map<string, InvocationState>,
  states: InvocationState[]
): Promise<void> {
  const state = invocations.get(message.invocation_id);
  const notebook = panel.content;
  const model = notebook.model;
  if (!state || !model || !message.source) {
    return;
  }

  const index = invocationInsertionIndex(panel, state, states);
  model.sharedModel.insertCell(index, {
    cell_type: 'markdown',
    source: message.source,
    metadata: {}
  });
  await Promise.resolve();
  const cell = notebook.widgets[index];
  if (cell instanceof MarkdownCell) {
    cell.rendered = true;
    state.lastModel = cell.model;
  }
}

function registerKernel(panel: NotebookPanel): void {
  const kernel = panel.sessionContext.session?.kernel;
  if (!kernel) {
    return;
  }

  let turn: TurnState | null = null;
  const invocations = new Map<string, InvocationState>();
  const invocationStates: InvocationState[] = [];

  const finishTurn = (): void => {
    if (!turn) {
      return;
    }
    const notebook = panel.content;
    const completed = turn;
    turn = null;
    if (completed.parentModel && completed.parentExecutionCount !== null) {
      completed.parentModel.executionCount = completed.parentExecutionCount;
    }
    if (completed.resumeModel) {
      const index = notebook.widgets.findIndex(
        widget => widget.model === completed.resumeModel
      );
      if (index >= 0) {
        notebook.activeCellIndex = index;
      }
    }
  };

  const beginTurn = (message: CodebindMessage): TurnState => {
    if (turn) {
      return turn;
    }
    const notebook = panel.content;
    const parentIndex = runningCellIndex(panel);
    const parentModel =
      parentIndex >= 0
        ? (notebook.widgets[parentIndex].model as ICodeCellModel)
        : null;
    turn = {
      parentModel,
      parentExecutionCount:
        message.type === 'code_cell' && message.execution_count !== null
          ? message.execution_count - 1
          : null,
      resumeModel: notebook.activeCell?.model ?? null,
      nextIndex:
        parentIndex >= 0
          ? parentIndex + 1
          : notebook.activeCell
            ? notebook.activeCellIndex + 1
            : 0
    };

    const onStatus = (
      _sender: Kernel.IKernelConnection,
      status: Kernel.Status
    ): void => {
      if (status === 'idle') {
        kernel.statusChanged.disconnect(onStatus);
        finishTurn();
      }
    };
    kernel.statusChanged.connect(onStatus);
    return turn;
  };

  kernel.registerCommTarget(
    TARGET_NAME,
    (comm: Kernel.IComm, _message: KernelMessage.ICommOpenMsg) => {
      comm.onMsg = (message: KernelMessage.ICommMsgMsg) => {
        const data = message.content.data;
        if (isCodebindMessage(data)) {
          const activeTurn = beginTurn(data);
          insertCell(panel, data, activeTurn.nextIndex);
          activeTurn.nextIndex += 1;
          return;
        }
        if (!isInvocationMessage(data)) {
          return;
        }
        if (data.type === 'invocation_started') {
          startInvocation(
            panel,
            comm,
            data,
            invocations,
            invocationStates
          );
        } else if (data.type === 'execute_cell') {
          void executeInvocationCell(
            panel,
            comm,
            data,
            invocations,
            invocationStates
          );
        } else {
          void completeInvocation(panel, data, invocations, invocationStates);
        }
      };
      comm.send({ type: 'ready' });
    }
  );
}

function connectPanel(panel: NotebookPanel): void {
  void panel.sessionContext.ready.then(() => registerKernel(panel));
  panel.sessionContext.kernelChanged.connect(() => registerKernel(panel));
}

const plugin: JupyterFrontEndPlugin<void> = {
  id: 'codebind-jupyterlab:plugin',
  description: 'Insert Codebind executions as native notebook cells.',
  autoStart: true,
  requires: [INotebookTracker],
  activate: (_app: JupyterFrontEnd, tracker: INotebookTracker): void => {
    tracker.forEach(connectPanel);
    tracker.widgetAdded.connect((_tracker, panel) => connectPanel(panel));
  }
};

export default plugin;
