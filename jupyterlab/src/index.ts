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
  return (
    message.type === 'code_cell' &&
    typeof message.source === 'string' &&
    (typeof message.execution_count === 'number' ||
      message.execution_count === null) &&
    Array.isArray(message.outputs)
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

function registerKernel(panel: NotebookPanel): void {
  const kernel = panel.sessionContext.session?.kernel;
  if (!kernel) {
    return;
  }

  let turn: TurnState | null = null;

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

  const beginTurn = (message: CodebindMessage): TurnState => {
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
