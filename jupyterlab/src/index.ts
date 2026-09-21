import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import { showErrorMessage, ToolbarButton } from '@jupyterlab/apputils';
import { ICellModel, ICodeCellModel, MarkdownCell } from '@jupyterlab/cells';
import * as nbformat from '@jupyterlab/nbformat';
import {
  INotebookTracker,
  NotebookActions,
  NotebookPanel
} from '@jupyterlab/notebook';
import { Kernel, KernelMessage } from '@jupyterlab/services';

const TARGET_NAME = 'codebind';
const INSERT_QUESTION = 'codebind:insert-question';
const RUN_QUESTION = 'codebind:run-question';
const QUESTION_CLASS = 'jp-CodebindQuestionCell';
const QUESTION_RUNNING_CLASS = 'jp-CodebindQuestionCell-running';
const QUESTION_ERROR_CLASS = 'jp-CodebindQuestionCell-error';

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

interface QuestionFinishedMessage {
  type: 'question_finished';
  cell_id: string;
  error: { type: string; message: string } | null;
}

type CodebindMessage =
  | CodeCellStartedMessage
  | CodeCellFinishedMessage
  | CodeCellOutputMessage
  | CodeCellClearMessage
  | MarkdownCellMessage
  | QuestionFinishedMessage;
type InsertMessage = CodeCellStartedMessage | MarkdownCellMessage;

interface TurnState {
  parentModel: ICodeCellModel | null;
  parentExecutionCount: number | null;
  resumeModel: ICellModel | null;
  nextIndex: number;
}

interface PanelState {
  comm: Kernel.IComm | null;
  runningQuestions: Map<string, ICellModel>;
}

interface QuestionMetadata {
  kind: 'question';
  model: string;
}

const panelStates = new WeakMap<NotebookPanel, PanelState>();

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
  if (message.type === 'question_finished') {
    const error = message.error;
    return (
      typeof message.cell_id === 'string' &&
      (error === null ||
        (typeof error === 'object' &&
          typeof (error as Record<string, unknown>).type === 'string' &&
          typeof (error as Record<string, unknown>).message === 'string'))
    );
  }
  return (
    message.type === 'code_cell_finished' &&
    typeof message.cell_id === 'string' &&
    (typeof message.execution_count === 'number' ||
      message.execution_count === null) &&
    Array.isArray(message.outputs)
  );
}

function questionMetadata(model: ICellModel): QuestionMetadata | null {
  const value = model.getMetadata('codebind');
  if (typeof value !== 'object' || value === null) {
    return null;
  }
  const metadata = value as Record<string, unknown>;
  if (metadata.kind !== 'question') {
    return null;
  }
  return {
    kind: 'question',
    model: typeof metadata.model === 'string' ? metadata.model : 'model'
  };
}

function refreshQuestionCells(panel: NotebookPanel): void {
  const state = panelStates.get(panel);
  for (const cell of panel.content.widgets) {
    const question = questionMetadata(cell.model);
    cell.node.classList.toggle(QUESTION_CLASS, question !== null);
    cell.node.classList.toggle(
      QUESTION_RUNNING_CLASS,
      state?.runningQuestions.has(cell.model.id) ?? false
    );
  }
}

function ensureStyles(): void {
  if (document.getElementById('codebind-question-styles')) {
    return;
  }
  const style = document.createElement('style');
  style.id = 'codebind-question-styles';
  style.textContent = `
    .${QUESTION_CLASS} {
      border-left: 3px solid var(--jp-brand-color1);
      background: color-mix(in srgb, var(--jp-brand-color1) 6%, transparent);
    }
    .${QUESTION_CLASS} .jp-Cell-inputWrapper::before {
      content: "Question";
      display: block;
      padding: 4px 10px 0;
      color: var(--jp-brand-color1);
      font-size: var(--jp-ui-font-size0);
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .${QUESTION_RUNNING_CLASS} {
      border-left-color: var(--jp-warn-color1);
      opacity: 0.78;
    }
    .${QUESTION_ERROR_CLASS} {
      border-left-color: var(--jp-error-color1);
    }
  `;
  document.head.appendChild(style);
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

  const panelState = panelStates.get(panel) ?? {
    comm: null,
    runningQuestions: new Map<string, ICellModel>()
  };
  panelStates.set(panel, panelState);
  panelState.comm = null;
  panelState.runningQuestions.clear();

  let turn: TurnState | null = null;
  const codeCells = new Map<string, ICodeCellModel>();

  const finishTurn = (restoreSelection = true): void => {
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
    if (restoreSelection && completed.resumeModel) {
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
        if (data.type === 'question_finished') {
          const model = panelState.runningQuestions.get(data.cell_id);
          panelState.runningQuestions.delete(data.cell_id);
          if (model) {
            const widget = panel.content.widgets.find(cell => cell.model === model);
            widget?.node.classList.toggle(
              QUESTION_ERROR_CLASS,
              data.error !== null
            );
          }
          refreshQuestionCells(panel);
          finishTurn(false);
          if (data.error) {
            void showErrorMessage(
              `Codebind question: ${data.error.type}`,
              data.error.message
            );
          }
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
      panelState.comm = comm;
      comm.onClose = () => {
        if (panelState.comm === comm) {
          panelState.comm = null;
        }
      };
      comm.send({ type: 'ready' });
    }
  );
}

function insertQuestion(panel: NotebookPanel): void {
  const notebook = panel.content;
  const model = notebook.model;
  if (!model) {
    return;
  }
  const index = notebook.activeCell ? notebook.activeCellIndex + 1 : 0;
  model.sharedModel.insertCell(index, {
    cell_type: 'markdown',
    source: '',
    metadata: {
      codebind: { kind: 'question', model: 'model' }
    }
  });
  notebook.activeCellIndex = index;
  notebook.deselectAll();
  const cell = notebook.widgets[index];
  if (cell instanceof MarkdownCell) {
    cell.rendered = false;
  }
  notebook.mode = 'edit';
  refreshQuestionCells(panel);
  cell?.editor?.focus();
}

async function runQuestion(panel: NotebookPanel): Promise<void> {
  const notebook = panel.content;
  const cell = notebook.activeCell;
  if (!cell) {
    return;
  }
  const metadata = questionMetadata(cell.model);
  if (!metadata) {
    return;
  }
  const state = panelStates.get(panel);
  if (!state?.comm) {
    await showErrorMessage(
      'Codebind is not connected',
      'Run %load_ext codebind in this kernel before sending a Question cell.'
    );
    return;
  }
  if (state.runningQuestions.size > 0) {
    await showErrorMessage(
      'Codebind is busy',
      'Wait for the current question to finish before sending another.'
    );
    return;
  }
  const question = cell.model.sharedModel.getSource().trim();
  if (!question) {
    await showErrorMessage('Empty Codebind question', 'Write a question before sending it.');
    return;
  }
  cell.node.classList.remove(QUESTION_ERROR_CLASS);
  state.runningQuestions.set(cell.model.id, cell.model);
  refreshQuestionCells(panel);
  if (cell instanceof MarkdownCell) {
    cell.rendered = true;
  }
  notebook.mode = 'command';
  state.comm.send({
    type: 'question',
    cell_id: cell.model.id,
    question,
    model: metadata.model
  });
}

function connectPanel(panel: NotebookPanel, app: JupyterFrontEnd): void {
  panelStates.set(panel, {
    comm: null,
    runningQuestions: new Map<string, ICellModel>()
  });
  panel.toolbar.insertItem(
    10,
    'codebindQuestion',
    new ToolbarButton({
      label: 'Question',
      tooltip: 'Insert a Codebind Question cell',
      onClick: () => {
        void app.commands.execute(INSERT_QUESTION);
      }
    })
  );
  panel.content.modelContentChanged.connect(() => refreshQuestionCells(panel));
  void panel.context.ready.then(() => refreshQuestionCells(panel));
  void panel.sessionContext.ready.then(() => registerKernel(panel));
  panel.sessionContext.kernelChanged.connect(() => registerKernel(panel));
}

const plugin: JupyterFrontEndPlugin<void> = {
  id: 'codebind-jupyterlab:plugin',
  description: 'Insert Codebind executions as native notebook cells.',
  autoStart: true,
  requires: [INotebookTracker],
  activate: (app: JupyterFrontEnd, tracker: INotebookTracker): void => {
    ensureStyles();
    app.commands.addCommand(INSERT_QUESTION, {
      label: 'Insert Codebind Question',
      execute: () => {
        const panel = tracker.currentWidget;
        if (panel) {
          insertQuestion(panel);
        }
      }
    });
    app.commands.addCommand(RUN_QUESTION, {
      label: 'Send Codebind Question',
      isEnabled: () => {
        const cell = tracker.currentWidget?.content.activeCell;
        return cell ? questionMetadata(cell.model) !== null : false;
      },
      execute: async () => {
        const panel = tracker.currentWidget;
        if (panel) {
          await runQuestion(panel);
        }
      }
    });
    app.commands.addKeyBinding({
      command: RUN_QUESTION,
      keys: ['Shift Enter'],
      selector: `.jp-Notebook .${QUESTION_CLASS}`
    });
    app.contextMenu.addItem({
      command: INSERT_QUESTION,
      selector: '.jp-Notebook .jp-Cell',
      rank: 8
    });
    tracker.forEach(panel => connectPanel(panel, app));
    tracker.widgetAdded.connect((_tracker, panel) => connectPanel(panel, app));
  }
};

export default plugin;
