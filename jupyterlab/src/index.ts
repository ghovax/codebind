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
const TOGGLE_QUESTION_MODE = 'codebind:toggle-question-mode';
const RUN_QUESTION = 'codebind:run-question';
const QUESTION_CLASS = 'jp-CodebindQuestionCell';

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

interface MarkdownCellStreamMessage {
  type:
    | 'markdown_cell_started'
    | 'markdown_cell_updated'
    | 'markdown_cell_finished';
  cell_id: string;
  source: string;
}

interface MarkdownCellCancelledMessage {
  type: 'markdown_cell_cancelled';
  cell_id: string;
}

interface InstructionsCellMessage {
  type: 'instructions_cell';
  source: string;
}

interface QuestionFinishedMessage {
  type: 'question_finished';
  cell_id: string;
  error: { type: string; message: string } | null;
  cancelled: boolean;
}

interface QuestionStartedMessage {
  type: 'question_started';
  cell_id: string;
}

interface SessionReadyMessage {
  type: 'session_ready';
  error: { type: string; message: string } | null;
}

interface ConversationSaveMessage {
  type: 'conversation_save';
  request_id: string;
  conversation: Record<string, unknown>;
}

type CodebindMessage =
  | CodeCellStartedMessage
  | CodeCellFinishedMessage
  | CodeCellOutputMessage
  | CodeCellClearMessage
  | MarkdownCellMessage
  | MarkdownCellStreamMessage
  | MarkdownCellCancelledMessage
  | InstructionsCellMessage
  | QuestionStartedMessage
  | QuestionFinishedMessage
  | SessionReadyMessage
  | ConversationSaveMessage;
type InsertMessage = CodeCellStartedMessage | MarkdownCellMessage;

interface TurnState {
  parentModel: ICodeCellModel | null;
  parentExecutionCount: number | null;
}

interface PanelState {
  comm: Kernel.IComm | null;
  kernel: Kernel.IKernelConnection | null;
  runningQuestions: Map<string, ICellModel>;
  questionTimers: Map<string, number>;
  sessionReady: boolean;
  sessionError: string | null;
  questionMode: boolean;
  questionButton: ToolbarButton | null;
  knownCellIds: Set<string>;
  pendingQuestionCellIds: Set<string>;
}

interface QuestionMetadata {
  kind: 'question';
  status: 'draft' | 'sent';
}

type JsonValue = string | number | boolean | null | JsonObject | JsonValue[];

interface JsonObject {
  [key: string]: JsonValue;
}

interface NotebookCellContext extends JsonObject {
  id: string;
  type: 'code' | 'markdown' | 'raw';
  source: string;
  execution_count: number | null;
  outputs: JsonObject[];
}

const panelStates = new WeakMap<NotebookPanel, PanelState>();
const kernelStates = new WeakMap<
  Kernel.IKernelConnection,
  Set<PanelState>
>();

function cancelQuestions(state: PanelState): boolean {
  if (state.runningQuestions.size === 0) {
    return false;
  }
  state.comm?.send({ type: 'cancel' });
  return true;
}

function isCodebindMessage(value: unknown): value is CodebindMessage {
  if (typeof value !== 'object' || value === null) {
    return false;
  }
  const message = value as Record<string, unknown>;
  if (
    message.type === 'markdown_cell' ||
    message.type === 'instructions_cell'
  ) {
    return typeof message.source === 'string';
  }
  if (
    message.type === 'markdown_cell_started' ||
    message.type === 'markdown_cell_updated' ||
    message.type === 'markdown_cell_finished'
  ) {
    return (
      typeof message.cell_id === 'string' && typeof message.source === 'string'
    );
  }
  if (message.type === 'markdown_cell_cancelled') {
    return typeof message.cell_id === 'string';
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
      typeof message.cancelled === 'boolean' &&
      (error === null ||
        (typeof error === 'object' &&
          typeof (error as Record<string, unknown>).type === 'string' &&
          typeof (error as Record<string, unknown>).message === 'string'))
    );
  }
  if (message.type === 'question_started') {
    return typeof message.cell_id === 'string';
  }
  if (message.type === 'session_ready') {
    const error = message.error;
    return (
      error === null ||
      (typeof error === 'object' &&
        typeof (error as Record<string, unknown>).type === 'string' &&
        typeof (error as Record<string, unknown>).message === 'string')
    );
  }
  if (message.type === 'conversation_save') {
    return (
      typeof message.request_id === 'string' &&
      typeof message.conversation === 'object' &&
      message.conversation !== null
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

function connectInterrupt(
  kernel: Kernel.IKernelConnection,
  state: PanelState
): void {
  if (state.kernel && state.kernel !== kernel) {
    kernelStates.get(state.kernel)?.delete(state);
  }
  state.kernel = kernel;
  let states = kernelStates.get(kernel);
  if (!states) {
    states = new Set<PanelState>();
    kernelStates.set(kernel, states);
    const interrupt = kernel.interrupt.bind(kernel);
    kernel.interrupt = async (): Promise<void> => {
      const active = [...(states ?? [])].filter(
        connected => connected.runningQuestions.size > 0
      );
      for (const connected of active) {
        cancelQuestions(connected);
      }
      for (let attempt = 0; active.length > 0 && attempt < 50; attempt += 1) {
        if (active.every(connected => connected.runningQuestions.size === 0)) {
          return;
        }
        await new Promise<void>(resolve => setTimeout(resolve, 10));
      }
      return interrupt();
    };
  }
  states.add(state);
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
    status: metadata.status === 'draft' ? 'draft' : 'sent'
  };
}

function codebindKind(model: ICellModel): string | null {
  const value = model.getMetadata('codebind');
  if (typeof value !== 'object' || value === null) {
    return null;
  }
  const kind = (value as Record<string, unknown>).kind;
  return typeof kind === 'string' ? kind : null;
}

function compactMimeBundle(bundle: nbformat.IMimeBundle): JsonObject {
  const data: JsonObject = {};
  const omitted: string[] = [];
  for (const [mime, value] of Object.entries(bundle)) {
    if (
      mime.startsWith('text/') ||
      mime === 'application/json' ||
      mime === 'application/vnd.jupyter.stdout' ||
      mime === 'application/vnd.jupyter.stderr'
    ) {
      if (value !== undefined) {
        data[mime] = value as JsonValue;
      }
    } else {
      omitted.push(mime);
    }
  }
  if (omitted.length > 0) {
    data.omitted_mime_types = omitted;
  }
  return data;
}

function compactOutput(output: nbformat.IOutput): JsonObject {
  if (nbformat.isStream(output)) {
    return { type: 'stream', name: output.name, text: output.text };
  }
  if (nbformat.isError(output)) {
    return {
      type: 'error',
      name: output.ename,
      message: output.evalue,
      traceback: output.traceback
    };
  }
  if (nbformat.isExecuteResult(output)) {
    return {
      type: 'result',
      execution_count: output.execution_count,
      data: compactMimeBundle(output.data)
    };
  }
  if (nbformat.isDisplayData(output)) {
    return { type: 'display', data: compactMimeBundle(output.data) };
  }
  return { type: output.output_type };
}

function notebookSnapshot(panel: NotebookPanel): NotebookCellContext[] {
  const cells: NotebookCellContext[] = [];
  for (const widget of panel.content.widgets) {
    if (codebindKind(widget.model) !== null) {
      continue;
    }
    const type = widget.model.type;
    if (type !== 'code' && type !== 'markdown' && type !== 'raw') {
      continue;
    }
    const cell: NotebookCellContext = {
      id: widget.model.id,
      type,
      source: widget.model.sharedModel.getSource(),
      execution_count: null,
      outputs: []
    };
    if (type === 'code') {
      const code = widget.model.toJSON() as nbformat.ICodeCell;
      cell.execution_count = code.execution_count;
      cell.outputs = code.outputs.map(compactOutput);
    }
    cells.push(cell);
  }
  return cells;
}

function normalizeMathDelimiters(source: string): string {
  return source.replace(/(^|[^\\])\\([()[\]])/g, '$1\\\\$2');
}

function refreshQuestionCells(panel: NotebookPanel): void {
  const state = panelStates.get(panel);
  for (const cell of panel.content.widgets) {
    const question = questionMetadata(cell.model);
    cell.node.classList.toggle(QUESTION_CLASS, question !== null);
    const prompt = cell.node.querySelector<HTMLElement>('.jp-InputPrompt');
    if (!prompt) {
      continue;
    }
    if (question) {
      cell.readOnly = question.status === 'sent';
      prompt.dataset.codebindQuestion = 'true';
      prompt.textContent = state?.runningQuestions.has(cell.model.id)
        ? '[*]:'
        : '[ ]:';
    } else if (prompt.dataset.codebindQuestion) {
      delete prompt.dataset.codebindQuestion;
      prompt.textContent = '';
    }
  }
}

function insertCell(
  panel: NotebookPanel,
  message: InsertMessage
): ICellModel | null {
  const notebook = panel.content;
  const model = notebook.model;
  if (!model) {
    return null;
  }

  const index = notebook.widgets.length;
  if (message.type === 'code_cell_started') {
    model.sharedModel.insertCell(index, {
      cell_type: 'code',
      source: message.source,
      metadata: {
        trusted: true,
        collapsed: true,
        editable: false,
        deletable: false,
        codebind: { kind: 'tool' }
      },
      execution_count: null,
      outputs: []
    });
  } else {
    model.sharedModel.insertCell(index, {
      cell_type: 'markdown',
      source: normalizeMathDelimiters(message.source),
      metadata: {
        editable: false,
        deletable: false,
        codebind: { kind: 'assistant' }
      }
    });
  }

  const cell = notebook.widgets[index] ?? null;
  if (cell) {
    cell.readOnly = true;
  }
  if (cell instanceof MarkdownCell) {
    cell.rendered = true;
  }
  return cell?.model ?? null;
}

function ensureInstructionsCell(panel: NotebookPanel, source: string): void {
  const notebook = panel.content;
  const model = notebook.model;
  if (!model) {
    return;
  }
  let changed = false;
  let index = notebook.widgets.findIndex(
    widget => codebindKind(widget.model) === 'instructions'
  );
  if (index < 0) {
    const activeId = notebook.activeCell?.model.id ?? null;
    model.sharedModel.insertCell(0, {
      cell_type: 'markdown',
      source,
      metadata: {
        editable: false,
        deletable: false,
        codebind: { kind: 'instructions' }
      }
    });
    index = 0;
    changed = true;
    if (activeId !== null) {
      const activeIndex = notebook.widgets.findIndex(
        widget => widget.model.id === activeId
      );
      if (activeIndex >= 0) {
        notebook.activeCellIndex = activeIndex;
      }
    }
  }
  const cell = notebook.widgets[index];
  if (!cell) {
    return;
  }
  if (cell.model.sharedModel.getSource() !== source) {
    cell.model.sharedModel.setSource(source);
    changed = true;
  }
  if (cell.model.getMetadata('editable') !== false) {
    changed = true;
  }
  if (cell.model.getMetadata('deletable') !== false) {
    changed = true;
  }
  cell.model.setMetadata('editable', false);
  cell.model.setMetadata('deletable', false);
  cell.model.setMetadata('codebind', { kind: 'instructions' });
  cell.readOnly = true;
  if (cell instanceof MarkdownCell) {
    cell.rendered = true;
  }
  if (changed) {
    void panel.context.save();
  }
}

function registerKernel(panel: NotebookPanel): void {
  const kernel = panel.sessionContext.session?.kernel;
  if (!kernel) {
    return;
  }

  const panelState = panelStates.get(panel) ?? {
    comm: null,
    kernel: null,
    runningQuestions: new Map<string, ICellModel>(),
    questionTimers: new Map<string, number>(),
    sessionReady: false,
    sessionError: null,
    questionMode: false,
    questionButton: null,
    knownCellIds: new Set(panel.content.widgets.map(widget => widget.model.id)),
    pendingQuestionCellIds: new Set<string>()
  };
  panelStates.set(panel, panelState);
  connectInterrupt(kernel, panelState);
  panelState.comm = null;
  panelState.runningQuestions.clear();
  for (const timer of panelState.questionTimers.values()) {
    window.clearTimeout(timer);
  }
  panelState.questionTimers.clear();
  panelState.sessionReady = false;
  panelState.sessionError = null;

  let turn: TurnState | null = null;
  const codeCells = new Map<string, ICodeCellModel>();
  const markdownCells = new Map<string, ICellModel>();

  const finishTurn = (): void => {
    if (!turn) {
      return;
    }
    const completed = turn;
    turn = null;
    if (
      completed.parentModel &&
      completed.parentExecutionCount !== null
    ) {
      completed.parentModel.executionCount = completed.parentExecutionCount;
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
      parentExecutionCount: null
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
        if (data.type === 'conversation_save') {
          const notebookModel = panel.content.model;
          if (!notebookModel) {
            comm.send({
              type: 'conversation_saved',
              request_id: data.request_id,
              error: 'Notebook model is unavailable.'
            });
            return;
          }
          notebookModel.setMetadata('codebind', data.conversation);
          void panel.context.save().then(
            () => {
              comm.send({
                type: 'conversation_saved',
                request_id: data.request_id,
                error: null
              });
            },
            error => {
              comm.send({
                type: 'conversation_saved',
                request_id: data.request_id,
                error: String(error)
              });
            }
          );
          return;
        }
        if (data.type === 'instructions_cell') {
          ensureInstructionsCell(panel, data.source);
          return;
        }
        if (data.type === 'session_ready') {
          panelState.sessionReady = data.error === null;
          panelState.sessionError = data.error?.message ?? null;
          if (data.error) {
            void showErrorMessage(
              `Codebind could not load: ${data.error.type}`,
              data.error.message
            );
          }
          return;
        }
        if (data.type === 'question_started') {
          const timer = panelState.questionTimers.get(data.cell_id);
          if (timer !== undefined) {
            window.clearTimeout(timer);
            panelState.questionTimers.delete(data.cell_id);
          }
          return;
        }
        if (data.type === 'question_finished') {
          const timer = panelState.questionTimers.get(data.cell_id);
          if (timer !== undefined) {
            window.clearTimeout(timer);
            panelState.questionTimers.delete(data.cell_id);
          }
          panelState.runningQuestions.delete(data.cell_id);
          refreshQuestionCells(panel);
          finishTurn();
          if (data.error && !data.cancelled) {
            void showErrorMessage(
              `Codebind question: ${data.error.type}`,
              data.error.message
            );
          }
          return;
        }
        if (data.type === 'markdown_cell_updated' || data.type === 'markdown_cell_finished') {
          const model = markdownCells.get(data.cell_id);
          if (!model) {
            return;
          }
          model.sharedModel.setSource(normalizeMathDelimiters(data.source));
          const cell = panel.content.widgets.find(widget => widget.model.id === model.id);
          if (cell instanceof MarkdownCell) {
            cell.rendered = true;
          }
          if (data.type === 'markdown_cell_finished') {
            markdownCells.delete(data.cell_id);
          }
          return;
        }
        if (data.type === 'markdown_cell_cancelled') {
          const model = markdownCells.get(data.cell_id);
          markdownCells.delete(data.cell_id);
          if (!model) {
            return;
          }
          const index = panel.content.widgets.findIndex(
            widget => widget.model.id === model.id
          );
          if (index >= 0) {
            panel.content.model?.sharedModel.deleteCell(index);
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

        let inserted: InsertMessage;
        if (data.type === 'markdown_cell_started') {
          inserted = { type: 'markdown_cell', source: data.source };
        } else if (
          data.type === 'markdown_cell' ||
          data.type === 'code_cell_started'
        ) {
          inserted = data;
        } else {
          return;
        }
        beginTurn();
        const model = insertCell(panel, inserted);
        if (data.type === 'code_cell_started' && model?.type === 'code') {
          const code = model as ICodeCellModel;
          code.executionState = 'running';
          codeCells.set(data.cell_id, code);
        }
        if (data.type === 'markdown_cell_started' && model) {
          markdownCells.set(data.cell_id, model);
        }
      };
      panelState.comm = comm;
      comm.onClose = () => {
        if (panelState.comm === comm) {
          panelState.comm = null;
          panelState.sessionReady = false;
        }
      };
      const conversation = panel.content.model?.getMetadata('codebind');
      comm.send({
        type: 'ready',
        conversation:
          typeof conversation === 'object' && conversation !== null
            ? conversation
            : null
      });
    }
  );
}

function configureQuestion(panel: NotebookPanel): void {
  const notebook = panel.content;
  const model = notebook.model;
  if (!model) {
    return;
  }
  if (!notebook.activeCell) {
    model.sharedModel.insertCell(0, {
      cell_type: 'markdown',
      source: '',
      metadata: {}
    });
    notebook.activeCellIndex = 0;
  } else if (notebook.activeCell.model.type !== 'markdown') {
    NotebookActions.changeCellType(notebook, 'markdown');
  }
  const cell = notebook.activeCell;
  if (cell && codebindKind(cell.model) !== null) {
    return;
  }
  cell?.model.setMetadata('codebind', { kind: 'question', status: 'draft' });
  if (cell instanceof MarkdownCell) {
    cell.rendered = false;
  }
  notebook.mode = 'edit';
  refreshQuestionCells(panel);
  cell?.editor?.focus();
}

function refreshQuestionMode(panel: NotebookPanel): void {
  const state = panelStates.get(panel);
  if (!state) {
    return;
  }
  const widgets = panel.content.widgets;
  const currentIds = new Set(widgets.map(widget => widget.model.id));
  const added = widgets.filter(widget => !state.knownCellIds.has(widget.model.id));
  state.knownCellIds = currentIds;
  for (const identifier of state.pendingQuestionCellIds) {
    if (!currentIds.has(identifier)) {
      state.pendingQuestionCellIds.delete(identifier);
    }
  }
  if (state.questionMode) {
    for (const widget of added) {
      if (codebindKind(widget.model) === null) {
        state.pendingQuestionCellIds.add(widget.model.id);
      }
    }
  }
  refreshQuestionCells(panel);
  if (!state.questionMode || !panel.content.activeCell) {
    return;
  }
  if (state.pendingQuestionCellIds.delete(panel.content.activeCell.model.id)) {
    configureQuestion(panel);
  }
}

function toggleQuestionMode(panel: NotebookPanel): void {
  const state = panelStates.get(panel);
  if (!state) {
    return;
  }
  setQuestionMode(panel, !state.questionMode);
}

function setQuestionMode(panel: NotebookPanel, enabled: boolean): void {
  const state = panelStates.get(panel);
  if (!state) {
    return;
  }
  state.questionMode = enabled;
  if (!state.questionMode) {
    state.pendingQuestionCellIds.clear();
  }
  if (state.questionButton) {
    state.questionButton.pressed = state.questionMode;
  }
  if (
    state.questionMode &&
    panel.content.activeCell &&
    codebindKind(panel.content.activeCell.model) === null
  ) {
    configureQuestion(panel);
  }
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
  for (
    let attempt = 0;
    !state.sessionReady && !state.sessionError && state.comm && attempt < 600;
    attempt += 1
  ) {
    await new Promise<void>(resolve => window.setTimeout(resolve, 50));
  }
  if (state.sessionError) {
    await showErrorMessage('Codebind could not load', state.sessionError);
    return;
  }
  if (!state.sessionReady) {
    await showErrorMessage(
      'Codebind did not finish loading',
      'Reload Codebind and send the Question again.'
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
  state.runningQuestions.set(cell.model.id, cell.model);
  cell.model.sharedModel.setSource(normalizeMathDelimiters(question));
  cell.model.setMetadata('codebind', { kind: 'question', status: 'sent' });
  cell.model.setMetadata('editable', false);
  cell.model.setMetadata('deletable', false);
  cell.readOnly = true;
  refreshQuestionCells(panel);
  if (cell instanceof MarkdownCell) {
    cell.rendered = true;
  }
  notebook.mode = 'command';
  const timer = window.setTimeout(() => {
    if (!state.questionTimers.has(cell.model.id)) {
      return;
    }
    state.questionTimers.delete(cell.model.id);
    state.runningQuestions.delete(cell.model.id);
    cell.model.setMetadata('codebind', { kind: 'question', status: 'draft' });
    cell.model.setMetadata('editable', true);
    cell.model.setMetadata('deletable', true);
    cell.readOnly = false;
    refreshQuestionCells(panel);
    void showErrorMessage(
      'Codebind did not receive the Question',
      'The kernel did not acknowledge it. Reload Codebind and send it again.'
    );
  }, 10_000);
  state.questionTimers.set(cell.model.id, timer);
  state.comm.send({
    type: 'question',
    cell_id: cell.model.id,
    question,
    notebook: notebookSnapshot(panel)
  });
}

function connectPanel(panel: NotebookPanel, app: JupyterFrontEnd): void {
  const state: PanelState = {
    comm: null,
    kernel: null,
    runningQuestions: new Map<string, ICellModel>(),
    questionTimers: new Map<string, number>(),
    sessionReady: false,
    sessionError: null,
    questionMode: false,
    questionButton: null,
    knownCellIds: new Set(panel.content.widgets.map(widget => widget.model.id)),
    pendingQuestionCellIds: new Set<string>()
  };
  panelStates.set(panel, state);
  const onInterrupt = (event: Event): void => {
    if (!cancelQuestions(state)) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    window.setTimeout(() => {
      if (state.runningQuestions.size > 0) {
        void state.kernel?.interrupt();
      }
    }, 500);
  };
  let interruptButton: HTMLElement | null = null;
  const connectInterruptButton = (): void => {
    const button = panel.toolbar.node.querySelector<HTMLElement>(
      '[data-command="notebook:interrupt-kernel"]'
    );
    if (button === interruptButton) {
      return;
    }
    interruptButton?.removeEventListener('click', onInterrupt, true);
    interruptButton = button;
    interruptButton?.addEventListener('click', onInterrupt, true);
  };
  const toolbarObserver = new MutationObserver(connectInterruptButton);
  toolbarObserver.observe(panel.toolbar.node, { childList: true, subtree: true });
  const cellObserver = new MutationObserver(() => {
    window.setTimeout(() => refreshQuestionMode(panel), 0);
  });
  cellObserver.observe(panel.content.node, { childList: true, subtree: true });
  connectInterruptButton();
  panel.disposed.connect(() => {
    toolbarObserver.disconnect();
    cellObserver.disconnect();
    interruptButton?.removeEventListener('click', onInterrupt, true);
    for (const timer of state.questionTimers.values()) {
      window.clearTimeout(timer);
    }
    state.questionTimers.clear();
  });
  const questionButton = new ToolbarButton({
    label: 'Question',
    tooltip: 'Toggle Question mode for the selected and newly created cells',
    onClick: () => {
      setQuestionMode(panel, questionButton.pressed);
      app.commands.notifyCommandChanged(TOGGLE_QUESTION_MODE);
    }
  });
  state.questionButton = questionButton;
  panel.toolbar.insertItem(
    10,
    'codebindQuestion',
    questionButton
  );
  panel.content.modelContentChanged.connect(() => {
    refreshQuestionMode(panel);
    window.setTimeout(() => refreshQuestionMode(panel), 0);
  });
  panel.content.activeCellChanged.connect(() => refreshQuestionMode(panel));
  void panel.context.ready.then(() => {
    panel.content.model?.cells.changed.connect(() => {
      refreshQuestionMode(panel);
      window.setTimeout(() => refreshQuestionMode(panel), 0);
    });
    refreshQuestionMode(panel);
  });
  void panel.sessionContext.ready.then(() => registerKernel(panel));
  panel.sessionContext.kernelChanged.connect(() => registerKernel(panel));
}

const plugin: JupyterFrontEndPlugin<void> = {
  id: 'codebind-jupyterlab:plugin',
  description: 'Insert Codebind executions as native notebook cells.',
  autoStart: true,
  requires: [INotebookTracker],
  activate: (app: JupyterFrontEnd, tracker: INotebookTracker): void => {
    app.commands.addCommand(INSERT_QUESTION, {
      label: 'Use Selected Cell as Codebind Question',
      execute: () => {
        const panel = tracker.currentWidget;
        if (panel) {
          configureQuestion(panel);
        }
      }
    });
    app.commands.addCommand(TOGGLE_QUESTION_MODE, {
      label: 'Toggle Codebind Question Mode',
      isToggled: () => {
        const panel = tracker.currentWidget;
        return panel ? (panelStates.get(panel)?.questionMode ?? false) : false;
      },
      execute: () => {
        const panel = tracker.currentWidget;
        if (panel) {
          toggleQuestionMode(panel);
          app.commands.notifyCommandChanged(TOGGLE_QUESTION_MODE);
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
