import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import { ICellModel, ICodeCellModel } from '@jupyterlab/cells';
import * as nbformat from '@jupyterlab/nbformat';
import {
  INotebookTracker,
  NotebookActions,
  NotebookPanel
} from '@jupyterlab/notebook';
import { Kernel, KernelMessage } from '@jupyterlab/services';

const TARGET_NAME = 'codebind';

interface CodeCellStartedMessage {
  type: 'code_cell_started';
  cell_id: string;
  source: string;
}

interface CodeCellFinishedMessage {
  type: 'code_cell_finished';
  cell_id: string;
  execution_count: number | null;
  outputs: nbformat.IOutput[];
}

interface CodeCellOutputMessage {
  type: 'code_cell_output';
  cell_id: string;
  output: nbformat.IOutput;
}

interface CodeCellClearMessage {
  type: 'code_cell_clear';
  cell_id: string;
  wait: boolean;
}

interface MarkdownCellMessage {
  type: 'markdown_cell';
  source: string;
}

type CodebindMessage =
  | CodeCellStartedMessage
  | CodeCellFinishedMessage
  | CodeCellOutputMessage
  | CodeCellClearMessage
  | MarkdownCellMessage;
type InsertMessage = CodeCellStartedMessage | MarkdownCellMessage;

interface TurnState {
  parentModel: ICodeCellModel | null;
  parentExecutionCount: number | null;
  resumeModel: ICellModel | null;
  nextIndex: number;
}

function isCodebindMessage(value: unknown): value is CodebindMessage {
  if (typeof value !== 'object' || value === null) {
    return false;
  }
  const message = value as Record<string, unknown>;
  if (message.type === 'markdown_cell') {
    return typeof message.source === 'string';
  }
  if (message.type === 'code_cell_started') {
    return (
      typeof message.cell_id === 'string' &&
      typeof message.source === 'string'
    );
  }
  if (message.type === 'code_cell_output') {
    return (
      typeof message.cell_id === 'string' &&
      typeof message.output === 'object' &&
      message.output !== null
    );
  }
  if (message.type === 'code_cell_clear') {
    return typeof message.cell_id === 'string' && typeof message.wait === 'boolean';
  }
  return (
    message.type === 'code_cell_finished' &&
    typeof message.cell_id === 'string' &&
    (typeof message.execution_count === 'number' ||
      message.execution_count === null) &&
    Array.isArray(message.outputs)
  );
}

function insertCell(
  panel: NotebookPanel,
  message: InsertMessage,
  index: number
): ICellModel | null {
  const notebook = panel.content;
  const model = notebook.model;
  if (!model) {
    return null;
  }

  if (message.type === 'code_cell_started') {
    model.sharedModel.insertCell(index, {
      cell_type: 'code',
      source: message.source,
      metadata: { trusted: true },
      execution_count: null,
      outputs: []
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
  return notebook.widgets[index]?.model ?? null;
}

function registerKernel(panel: NotebookPanel): void {
  const kernel = panel.sessionContext.session?.kernel;
  if (!kernel) {
    return;
  }

  let turn: TurnState | null = null;
  const codeCells = new Map<string, ICodeCellModel>();

  const finishTurn = (): void => {
    if (!turn) {
      return;
    }
    const notebook = panel.content;
    const completed = turn;
    turn = null;
    if (
      completed.parentModel &&
      completed.parentExecutionCount !== null
    ) {
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

  const beginTurn = (): TurnState => {
    if (turn) {
      return turn;
    }
    const notebook = panel.content;
    const parentIndex = notebook.widgets.findIndex(
      widget =>
        widget.model.type === 'code' &&
        (widget.model as ICodeCellModel).executionState === 'running'
    );
    const parentModel =
      parentIndex >= 0
        ? (notebook.widgets[parentIndex].model as ICodeCellModel)
        : null;
    turn = {
      parentModel,
      parentExecutionCount: null,
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
        if (!isCodebindMessage(data)) {
          return;
        }
        if (data.type === 'code_cell_output') {
          codeCells.get(data.cell_id)?.outputs.add(data.output);
          return;
        }
        if (data.type === 'code_cell_clear') {
          codeCells.get(data.cell_id)?.outputs.clear(data.wait);
          return;
        }
        if (data.type === 'code_cell_finished') {
          const model = codeCells.get(data.cell_id);
          if (!model) {
            return;
          }
          model.outputs.fromJSON(data.outputs);
          model.executionCount = data.execution_count;
          model.executionState = 'idle';
          if (
            turn &&
            turn.parentExecutionCount === null &&
            data.execution_count !== null
          ) {
            turn.parentExecutionCount = data.execution_count - 1;
          }
          return;
        }

        const activeTurn = beginTurn();
        const model = insertCell(panel, data, activeTurn.nextIndex);
        if (data.type === 'code_cell_started' && model?.type === 'code') {
          const code = model as ICodeCellModel;
          code.executionState = 'running';
          codeCells.set(data.cell_id, code);
        }
        activeTurn.nextIndex += 1;
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
