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
import {
  CommsOverSubshells,
  Kernel,
  KernelMessage
} from '@jupyterlab/services';

const TARGET_NAME = 'codebind';
const INSERT_QUESTION = 'codebind:insert-question';
const TOGGLE_QUESTION_MODE = 'codebind:toggle-question-mode';
const RUN_QUESTION = 'codebind:run-question';
const QUESTION_CLASS = 'jp-CodebindQuestionCell';
const CANCEL_GRACE_MILLISECONDS = 3000;

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

interface CodeCellCancelledMessage {
  type: 'code_cell_cancelled';
  cell_id: string;
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

interface CancelAcknowledgedMessage {
  type: 'cancel_acknowledged';
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
  | CodeCellCancelledMessage
  | CodeCellOutputMessage
  | CodeCellClearMessage
  | MarkdownCellMessage
  | MarkdownCellStreamMessage
  | MarkdownCellCancelledMessage
  | QuestionStartedMessage
  | CancelAcknowledgedMessage
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
  registeredKernel: Kernel.IKernelConnection | null;
  connectKernel: (() => void) | null;
  disconnectKernel: (() => void) | null;
  ready: Promise<void> | null;
  resolveReady: (() => void) | null;
  rejectReady: ((error: Error) => void) | null;
  connectOnIdle: boolean;
  runningQuestions: Map<string, ICellModel>;
  questionTimers: Map<string, number>;
  sessionReady: boolean;
  cancelAcknowledged: boolean;
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
  attachments: JsonObject;
}

const panelStates = new WeakMap<NotebookPanel, PanelState>();
const kernelStates = new WeakMap<
  Kernel.IKernelConnection,
  Set<PanelState>
>();

function isCodebindMessage(value: unknown): value is CodebindMessage {
  if (typeof value !== 'object' || value === null) {
    return false;
  }
  const message = value as Record<string, unknown>;
  if (message.type === 'markdown_cell') {
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
  if (message.type === 'code_cell_cancelled') {
    return typeof message.cell_id === 'string';
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
  if (message.type === 'cancel_acknowledged') {
    return true;
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
        connected.cancelAcknowledged = false;
      }
      if (active.length > 0) {
        try {
          const request = KernelMessage.createMessage<KernelMessage.IInterruptRequestMsg>({
            session: kernel.clientId,
            username: kernel.username,
            channel: 'control',
            msgType: 'interrupt_request',
            content: {}
          });
          const future = kernel.sendControlMessage(request, true);
          void future.done.catch(() => undefined);
        } catch {
          // The standard kernel interrupt below remains the fallback.
        }
      }
      for (
        let attempt = 0;
        active.length > 0 && attempt < CANCEL_GRACE_MILLISECONDS / 20;
        attempt += 1
      ) {
        if (
          active.every(
            connected =>
              connected.runningQuestions.size === 0 || connected.cancelAcknowledged
          )
        ) {
          return;
        }
        await new Promise<void>(resolve => setTimeout(resolve, 20));
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
      [
        'image/png',
        'image/jpeg',
        'image/webp',
        'image/gif',
        'image/svg+xml'
      ].includes(mime) ||
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
      outputs: [],
      attachments: {}
    };
    if (type === 'code') {
      const code = widget.model.toJSON() as nbformat.ICodeCell;
      cell.execution_count = code.execution_count;
      cell.outputs = code.outputs.map(compactOutput);
    } else {
      const attachments = (widget.model.toJSON() as nbformat.IMarkdownCell)
        .attachments;
      if (attachments) {
        const compact: JsonObject = {};
        for (const [name, bundle] of Object.entries(attachments)) {
          if (bundle) {
            compact[name] = compactMimeBundle(bundle);
          }
        }
        cell.attachments = compact;
      }
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
      prompt.style.color = '#a855f7';
      prompt.textContent = state?.runningQuestions.has(cell.model.id)
        ? '[*]:'
        : '[ ]:';
    } else if (prompt.dataset.codebindQuestion) {
      delete prompt.dataset.codebindQuestion;
      prompt.style.removeProperty('color');
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

function hideInstructionsCells(panel: NotebookPanel): void {
  const notebook = panel.content;
  for (const cell of notebook.widgets) {
    if (codebindKind(cell.model) === 'instructions') {
      cell.node.hidden = true;
    }
  }
  if (notebook.activeCell && codebindKind(notebook.activeCell.model) === 'instructions') {
    const next = notebook.widgets.findIndex(
      cell => codebindKind(cell.model) !== 'instructions'
    );
    if (next >= 0) {
      notebook.activeCellIndex = next;
    }
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
    registeredKernel: null,
    connectKernel: null,
    disconnectKernel: null,
    ready: null,
    resolveReady: null,
    rejectReady: null,
    connectOnIdle: false,
    runningQuestions: new Map<string, ICellModel>(),
    questionTimers: new Map<string, number>(),
    sessionReady: false,
    cancelAcknowledged: false,
    questionMode: false,
    questionButton: null,
    knownCellIds: new Set(panel.content.widgets.map(widget => widget.model.id)),
    pendingQuestionCellIds: new Set<string>()
  };
  panelStates.set(panel, panelState);
  if (panelState.registeredKernel === kernel) {
    return;
  }
  panelState.disconnectKernel?.();
  panelState.rejectReady?.(new Error('The notebook kernel changed.'));
  panelState.comm?.close();
  panelState.registeredKernel = kernel;
  panelState.connectKernel = null;
  panelState.disconnectKernel = null;
  panelState.ready = null;
  panelState.resolveReady = null;
  panelState.rejectReady = null;
  panelState.connectOnIdle = false;
  connectInterrupt(kernel, panelState);
  panelState.comm = null;
  panelState.runningQuestions.clear();
  for (const timer of panelState.questionTimers.values()) {
    window.clearTimeout(timer);
  }
  panelState.questionTimers.clear();
  panelState.sessionReady = false;
  panelState.cancelAcknowledged = false;

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

  const connectKernel = (): void => {
    if (panelState.comm || kernel.status !== 'idle' || !panel.content.model) {
      return;
    }
    const comm = kernel.createComm(TARGET_NAME);
    comm.commsOverSubshells = CommsOverSubshells.Disabled;
    panelState.comm = comm;
    panelState.sessionReady = false;
    panelState.ready = new Promise<void>((resolve, reject) => {
      panelState.resolveReady = resolve;
      panelState.rejectReady = reject;
    });
    void panelState.ready.catch(() => undefined);
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
      if (data.type === 'session_ready') {
        panelState.sessionReady = data.error === null;
        if (data.error) {
          panelState.rejectReady?.(new Error(data.error.message));
          void showErrorMessage(
            `Codebind could not load: ${data.error.type}`,
            data.error.message
          );
        } else {
          panelState.resolveReady?.();
        }
        panelState.resolveReady = null;
        panelState.rejectReady = null;
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
      if (data.type === 'cancel_acknowledged') {
        panelState.cancelAcknowledged = true;
        return;
      }
      if (data.type === 'question_finished') {
        const timer = panelState.questionTimers.get(data.cell_id);
        if (timer !== undefined) {
          window.clearTimeout(timer);
          panelState.questionTimers.delete(data.cell_id);
        }
        panelState.runningQuestions.delete(data.cell_id);
        if (panelState.runningQuestions.size === 0) {
          panelState.cancelAcknowledged = false;
        }
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
      if (
        data.type === 'markdown_cell_updated' ||
        data.type === 'markdown_cell_finished'
      ) {
        const model = markdownCells.get(data.cell_id);
        if (!model) {
          return;
        }
        model.sharedModel.setSource(normalizeMathDelimiters(data.source));
        const cell = panel.content.widgets.find(
          widget => widget.model.id === model.id
        );
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
        codeCells.delete(data.cell_id);
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
      if (data.type === 'code_cell_cancelled') {
        const model = codeCells.get(data.cell_id);
        codeCells.delete(data.cell_id);
        if (model) {
          model.executionState = 'idle';
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
    comm.onClose = () => {
      if (panelState.comm === comm) {
        const wasReady = panelState.sessionReady;
        for (const model of codeCells.values()) {
          model.executionState = 'idle';
        }
        codeCells.clear();
        panelState.runningQuestions.clear();
        panelState.cancelAcknowledged = false;
        for (const timer of panelState.questionTimers.values()) {
          window.clearTimeout(timer);
        }
        panelState.questionTimers.clear();
        refreshQuestionCells(panel);
        panelState.rejectReady?.(
          new Error(
            'Codebind is not loaded in this kernel. Run %load_ext codebind.'
          )
        );
        panelState.comm = null;
        panelState.sessionReady = false;
        panelState.ready = null;
        panelState.resolveReady = null;
        panelState.rejectReady = null;
        panelState.connectOnIdle = wasReady;
      }
    };
    const conversation = panel.content.model?.getMetadata('codebind');
    comm.open({
      conversation:
        typeof conversation === 'object' && conversation !== null
          ? conversation
          : null
    });
  };
  panelState.connectKernel = connectKernel;
  const onStatus = (
    _sender: Kernel.IKernelConnection,
    status: Kernel.Status
  ): void => {
    if (status === 'idle' && panelState.connectOnIdle) {
      panelState.connectOnIdle = false;
      connectKernel();
    }
  };
  const onInput = (
    _sender: Kernel.IKernelConnection,
    message: KernelMessage.IIOPubMessage
  ): void => {
    if (
      KernelMessage.isExecuteInputMsg(message) &&
      /(?:^|\n)\s*%(?:re)?load_ext\s+codebind(?:\s|$)/.test(message.content.code)
    ) {
      panelState.connectOnIdle = true;
    }
  };
  kernel.statusChanged.connect(onStatus);
  kernel.iopubMessage.connect(onInput);
  panelState.disconnectKernel = () => {
    kernel.statusChanged.disconnect(onStatus);
    kernel.iopubMessage.disconnect(onInput);
  };
  void panel.context.ready.then(() => {
    if (panel.content.model?.getMetadata('codebind')) {
      panelState.connectOnIdle = true;
      connectKernel();
    }
  });
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
  hideInstructionsCells(panel);
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
    const active = panel.content.activeCell;
    if (active && questionMetadata(active.model)?.status === 'draft') {
      active.model.deleteMetadata('codebind');
      NotebookActions.changeCellType(panel.content, 'code');
      panel.content.mode = 'edit';
      panel.content.activeCell?.editor?.focus();
    }
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
  await panel.sessionContext.ready;
  registerKernel(panel);
  const state = panelStates.get(panel);
  const kernel = state?.registeredKernel;
  if (!state || !kernel) {
    await showErrorMessage(
      'Codebind is not connected',
      'Start the notebook kernel before sending a Question cell.'
    );
    return;
  }
  try {
    if (kernel.status === 'dead') {
      throw new Error('The notebook kernel stopped.');
    }
    if (kernel.status !== 'idle') {
      await new Promise<void>((resolve, reject) => {
        const onStatus = (
          _sender: Kernel.IKernelConnection,
          status: Kernel.Status
        ): void => {
          if (status === 'idle' || status === 'dead') {
            kernel.statusChanged.disconnect(onStatus);
            if (status === 'idle') {
              resolve();
            } else {
              reject(new Error('The notebook kernel stopped.'));
            }
          }
        };
        kernel.statusChanged.connect(onStatus);
      });
    }
    state.connectKernel?.();
    if (!state.ready) {
      throw new Error('Run %load_ext codebind in this kernel.');
    }
    await state.ready;
  } catch (error) {
    await showErrorMessage('Codebind could not load', String(error));
    return;
  }
  if (!state.sessionReady || !state.comm) {
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
  state.runningQuestions.set(cell.model.id, cell.model);
  state.cancelAcknowledged = false;
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
    registeredKernel: null,
    connectKernel: null,
    disconnectKernel: null,
    ready: null,
    resolveReady: null,
    rejectReady: null,
    connectOnIdle: false,
    runningQuestions: new Map<string, ICellModel>(),
    questionTimers: new Map<string, number>(),
    sessionReady: false,
    cancelAcknowledged: false,
    questionMode: false,
    questionButton: null,
    knownCellIds: new Set(panel.content.widgets.map(widget => widget.model.id)),
    pendingQuestionCellIds: new Set<string>()
  };
  panelStates.set(panel, state);
  let interruptPending = false;
  const requestInterrupt = (): void => {
    if (interruptPending) {
      return;
    }
    if (!state.kernel) {
      if (state.runningQuestions.size > 0) {
        state.cancelAcknowledged = false;
        state.comm?.send({ type: 'cancel' });
      }
      return;
    }
    interruptPending = true;
    void state.kernel
      .interrupt()
      .catch(error => {
        if (state.runningQuestions.size > 0) {
          void showErrorMessage('Codebind could not interrupt', String(error));
        }
      })
      .finally(() => {
        interruptPending = false;
      });
  };
  const onInterrupt = (event: Event): void => {
    if (state.runningQuestions.size === 0) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    requestInterrupt();
  };
  const onKeyDown = (event: KeyboardEvent): void => {
    if (
      event.key === 'Tab' &&
      event.shiftKey &&
      !event.ctrlKey &&
      !event.altKey &&
      !event.metaKey &&
      event.target instanceof Node &&
      panel.content.node.contains(event.target)
    ) {
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      toggleQuestionMode(panel);
      app.commands.notifyCommandChanged(TOGGLE_QUESTION_MODE);
    }
  };
  const onGlobalKeyDown = (event: KeyboardEvent): void => {
    if (
      state.runningQuestions.size === 0 ||
      app.shell.currentWidget !== panel ||
      event.key.toLowerCase() !== 'c' ||
      !event.ctrlKey ||
      event.altKey ||
      event.metaKey ||
      event.shiftKey
    ) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    requestInterrupt();
  };
  panel.node.addEventListener('keydown', onKeyDown, true);
  window.addEventListener('keydown', onGlobalKeyDown, true);
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
    panel.node.removeEventListener('keydown', onKeyDown, true);
    window.removeEventListener('keydown', onGlobalKeyDown, true);
    state.disconnectKernel?.();
    state.rejectReady?.(new Error('The notebook closed.'));
    state.comm?.close();
    if (state.kernel) {
      kernelStates.get(state.kernel)?.delete(state);
    }
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
    hideInstructionsCells(panel);
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
      selector: `.jp-Notebook.jp-mod-editMode .${QUESTION_CLASS}`
    });
    app.commands.addKeyBinding({
      command: RUN_QUESTION,
      keys: ['Shift Enter'],
      selector: `.jp-Notebook.jp-mod-commandMode .${QUESTION_CLASS}`
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
