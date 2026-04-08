import type { KeyboardEvent } from "react";
import type { SessionMessage, TaskStep } from "../../types";
import type { RightColumnProps, StatusTone } from "./types";

type PipelineRow = {
  title: string;
  tone: StatusTone;
  statusLabel: string;
  progress: number | null;
};

type TaskTreeNode = {
  task_id: string;
  parent_task_id?: string | null;
  status: string;
  current_step?: string | null;
  analysis_type: string;
  created_at?: string | null;
  task_spec?: { aoi_input?: string | null } | null;
  children: TaskTreeNode[];
};

function toTimestamp(value?: string | null): number {
  if (!value) {
    return 0;
  }
  const timestamp = Date.parse(value);
  return Number.isNaN(timestamp) ? 0 : timestamp;
}

function buildTaskTree(entries: RightColumnProps["taskHistory"]): TaskTreeNode[] {
  const byId = new Map<string, TaskTreeNode>();
  for (const entry of entries) {
    byId.set(entry.task_id, { ...entry, children: [] });
  }

  const roots: TaskTreeNode[] = [];
  for (const node of byId.values()) {
    if (node.parent_task_id && byId.has(node.parent_task_id)) {
      byId.get(node.parent_task_id)?.children.push(node);
    } else {
      roots.push(node);
    }
  }

  const sortNodes = (nodes: TaskTreeNode[]): void => {
    nodes.sort((a, b) => toTimestamp(b.created_at) - toTimestamp(a.created_at));
    for (const node of nodes) {
      sortNodes(node.children);
    }
  };

  sortNodes(roots);
  return roots;
}

function taskStatusLabel(status: string): string {
  switch (status) {
    case "queued":
      return "排队";
    case "running":
      return "执行中";
    case "success":
      return "完成";
    case "failed":
      return "失败";
    case "awaiting_approval":
      return "待审批";
    case "waiting_clarification":
      return "待澄清";
    case "cancelled":
      return "已取消";
    default:
      return status;
  }
}

function taskStatusToneClass(status: string): string {
  if (status === "success") {
    return "tone-success";
  }
  if (status === "failed" || status === "cancelled") {
    return "tone-failed";
  }
  if (status === "running" || status === "queued") {
    return "tone-running";
  }
  return "tone-warn";
}

function extractProgress(step: TaskStep): number | null {
  const payload = step.observation ?? step.detail;
  if (!payload) {
    return null;
  }

  const value =
    payload.progress_pct ?? payload.progress ?? payload.percent ?? payload.percentage ?? payload.ratio ?? payload.completed_ratio;

  if (typeof value !== "number" || Number.isNaN(value)) {
    return null;
  }
  const normalized = value <= 1 ? value * 100 : value;
  return Math.max(0, Math.min(100, normalized));
}

function buildPipelineRows(
  steps: TaskStep[],
  formatStatusTone: (status?: string | null) => StatusTone,
  formatStatusLabel: (status?: string | null) => string,
): PipelineRow[] {
  const rows = steps.slice(-4).map((step) => {
    const tone = formatStatusTone(step.status);
    const progress = tone === "running" ? extractProgress(step) : null;

    return {
      title: step.step_name,
      tone,
      statusLabel: tone === "running" && progress !== null ? `执行中 ${Math.round(progress)}%` : formatStatusLabel(step.status),
      progress,
    };
  });

  if (rows.length) {
    return rows;
  }

  return [
    { title: "等待任务创建", tone: "idle", statusLabel: "待处理", progress: null },
    { title: "等待计划审批", tone: "idle", statusLabel: "待处理", progress: null },
  ];
}

function messageRoleLabel(role: string): string {
  if (role === "assistant") {
    return "助手";
  }
  if (role === "system") {
    return "系统";
  }
  return "你";
}

function bubbleClass(role: string): string {
  if (role === "assistant") {
    return "wb-chat-bubble wb-chat-bubble-assistant";
  }
  if (role === "system") {
    return "wb-chat-bubble wb-chat-bubble-system";
  }
  return "wb-chat-bubble wb-chat-bubble-user";
}

function buildSyntheticTaskMessages(currentTask: RightColumnProps["currentTask"]): SessionMessage[] {
  if (!currentTask) {
    return [];
  }

  const synthetic: SessionMessage[] = [];

  if (currentTask.status === "success") {
    synthetic.push({
      message_id: `task-success-${currentTask.task_id}`,
      role: "assistant",
      content: currentTask.summary_text ?? "任务已成功完成，可以下载产物或继续追问重跑。",
      linked_task_id: currentTask.task_id,
      created_at: currentTask.created_at,
    });
  }

  if (currentTask.status === "failed") {
    synthetic.push({
      message_id: `task-failed-${currentTask.task_id}`,
      role: "assistant",
      content:
        currentTask.error_message ??
        (currentTask.error_code ? `任务执行失败：${currentTask.error_code}` : "任务执行失败，请调整方案后重试。"),
      linked_task_id: currentTask.task_id,
      created_at: currentTask.created_at,
    });
  }

  if (currentTask.status === "cancelled") {
    synthetic.push({
      message_id: `task-cancelled-${currentTask.task_id}`,
      role: "assistant",
      content: currentTask.rejected_reason
        ? `计划已拒绝：${currentTask.rejected_reason}。你可以继续对话生成新的处理方案。`
        : "计划已拒绝。你可以继续对话生成新的处理方案。",
      linked_task_id: currentTask.task_id,
      created_at: currentTask.rejected_at ?? currentTask.created_at,
    });
  }

  return synthetic;
}

export default function RightColumn({
  currentTaskId,
  taskHistory,
  currentTask,
  sessionMessages,
  streamingAssistantMessage,
  messagesNextCursor,
  isLoadingMessages,
  messagesError,
  streamMode,
  composerText,
  setComposerText,
  submitError,
  notice,
  planError,
  isSubmitting,
  isSavingPlan,
  isApprovingPlan,
  isRejectingPlan,
  planDraftText,
  setPlanDraftText,
  handleSubmit,
  handleSavePlanDraft,
  handleApprovePlan,
  handleRejectPlan,
  handleLoadMoreMessages,
  onHistorySelect,
  canEditOperationPlan,
  canApproveOperationPlan,
  formatStatusLabel,
  formatStatusTone,
  formatDateTime,
}: RightColumnProps) {
  const pipelineRows = buildPipelineRows(currentTask?.steps ?? [], formatStatusTone, formatStatusLabel);
  const syntheticTaskMessages = buildSyntheticTaskMessages(currentTask);
  const conversation = [...sessionMessages, ...syntheticTaskMessages];
  const showTypingIndicator = isSubmitting && (!streamingAssistantMessage || streamingAssistantMessage.length === 0);
  const taskTree = buildTaskTree(taskHistory);

  const handleComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) {
      return;
    }
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  };

  const renderTaskNode = (node: TaskTreeNode, depth: number) => {
    const title = node.task_spec?.aoi_input ?? `${node.analysis_type} · ${node.task_id.slice(-6)}`;
    return (
      <div key={node.task_id} className="wb-session-node-wrap">
        <button
          type="button"
          className={`wb-session-node ${currentTaskId === node.task_id ? "is-active" : ""}`}
          style={{ paddingLeft: `${10 + depth * 14}px` }}
          onClick={() => onHistorySelect(node.task_id)}
        >
          <div className="wb-session-node-head">
            <strong>{title}</strong>
            <span className={`wb-session-status ${taskStatusToneClass(node.status)}`}>{taskStatusLabel(node.status)}</span>
          </div>
          <p>
            {node.task_id}
            {node.current_step ? ` · ${node.current_step}` : ""}
          </p>
        </button>

        {node.children.length ? <div className="wb-session-children">{node.children.map((child) => renderTaskNode(child, depth + 1))}</div> : null}
      </div>
    );
  };

  return (
    <aside className="wb-right-anchor">
      <div className="wb-chat-head">
        <div className="wb-workbench-icon-wrap">
          <span className="material-symbols-outlined">chat</span>
        </div>
        <div>
          <h2>智能助手</h2>
          <p>{currentTask ? `${formatStatusLabel(currentTask.status)} · ${currentTask.current_step ?? "等待输入"}` : "等待对话输入"}</p>
        </div>
      </div>

      <div className="wb-chat-scroll">
        <section className="wb-chat-card">
          <h3>会话树</h3>
          {taskTree.length ? (
            <div className="wb-session-tree">{taskTree.map((node) => renderTaskNode(node, 0))}</div>
          ) : (
            <p className="wb-panel-note">当前 session 还没有任务会话，先在下方发起对话。</p>
          )}
        </section>

        {messagesNextCursor ? (
          <button
            className="wb-chat-load-more"
            type="button"
            disabled={isLoadingMessages}
            onClick={() => {
              void handleLoadMoreMessages();
            }}
          >
            {isLoadingMessages ? "加载中..." : "加载更早对话"}
          </button>
        ) : null}

        {messagesError ? <p className="wb-panel-note wb-note-error">{messagesError}</p> : null}

        {conversation.length || streamingAssistantMessage ? (
          <div className="wb-chat-list">
            {conversation.map((message) => (
              <article key={message.message_id} className={bubbleClass(message.role)}>
                <header>
                  <span>{messageRoleLabel(message.role)}</span>
                  <time>{formatDateTime(message.created_at)}</time>
                </header>
                <pre>{message.content}</pre>
                {message.linked_task_id ? <small>任务: {message.linked_task_id}</small> : null}
              </article>
            ))}

            {showTypingIndicator ? (
              <article className="wb-chat-bubble wb-chat-bubble-assistant wb-chat-typing-indicator" aria-live="polite" aria-label="助手正在输入">
                <div className="wb-typing-dots" aria-hidden="true">
                  <span className="wb-typing-dot" />
                  <span className="wb-typing-dot" />
                  <span className="wb-typing-dot" />
                </div>
              </article>
            ) : null}

            {streamingAssistantMessage ? (
              <article className="wb-chat-bubble wb-chat-bubble-assistant wb-chat-bubble-streaming">
                <header>
                  <span>助手</span>
                  <time>正在输出...</time>
                </header>
                <pre>{streamingAssistantMessage}</pre>
              </article>
            ) : null}
          </div>
        ) : (
          <p className="wb-panel-note">还没有对话记录，先在下方输入处理需求。</p>
        )}

        {currentTask?.status === "awaiting_approval" ? (
          <section className="wb-chat-card">
            <h3>计划审批</h3>
            <p>
              版本 v{currentTask.operation_plan?.version ?? "-"} · 先审阅再执行。你可以修改计划、拒绝计划，或批准执行。
            </p>

            <textarea
              className="wb-chat-plan-editor"
              value={planDraftText}
              onChange={(event) => setPlanDraftText(event.target.value)}
              spellCheck={false}
            />

            <div className="wb-chat-plan-actions">
              <button
                type="button"
                disabled={!canEditOperationPlan || isSavingPlan || isApprovingPlan || isRejectingPlan}
                onClick={() => {
                  void handleSavePlanDraft();
                }}
              >
                {isSavingPlan ? "保存中..." : "更新计划"}
              </button>

              <button
                type="button"
                className="wb-chat-reject-btn"
                disabled={!canEditOperationPlan || isSavingPlan || isApprovingPlan || isRejectingPlan}
                onClick={() => {
                  void handleRejectPlan("rejected_by_user");
                }}
              >
                {isRejectingPlan ? "拒绝中..." : "拒绝计划"}
              </button>

              <button
                type="button"
                className="wb-chat-approve-btn"
                disabled={!canApproveOperationPlan || isSavingPlan || isApprovingPlan || isRejectingPlan}
                onClick={() => {
                  void handleApprovePlan();
                }}
              >
                {isApprovingPlan ? "审批中..." : "批准并执行"}
              </button>
            </div>

            {planError ? <p className="wb-panel-note wb-note-error">{planError}</p> : null}
          </section>
        ) : null}

        {currentTask ? (
          <section className="wb-chat-card">
            <h3>执行流水</h3>
            <div className="wb-chat-pipeline">
              {pipelineRows.map((row) => (
                <article key={row.title} className={`wb-chat-pipeline-row tone-${row.tone}`}>
                  <div className="wb-chat-pipeline-head">
                    <strong>{row.title}</strong>
                    <span>{row.statusLabel}</span>
                  </div>
                  {row.progress !== null ? (
                    <div className="wb-chat-progress-track">
                      <div className="wb-chat-progress-fill" style={{ width: `${Math.round(row.progress)}%` }} />
                    </div>
                  ) : null}
                </article>
              ))}
            </div>
          </section>
        ) : null}
      </div>

      <footer className="wb-chat-composer">
        {notice ? <p className="wb-panel-note wb-note-info">{notice}</p> : null}
        {submitError ? <p className="wb-panel-note wb-note-error">{submitError}</p> : null}

        <form onSubmit={handleSubmit}>
          <textarea
            value={composerText}
            onChange={(event) => setComposerText(event.target.value)}
            onKeyDown={handleComposerKeyDown}
            placeholder="先描述 GIS 处理需求。进入执行阶段后，系统会创建计划供你审批。"
          />

          <div className="wb-chat-composer-actions">
            <button className="wb-chat-send-btn" type="submit" disabled={isSubmitting}>
              {isSubmitting ? "发送中..." : "发送"}
            </button>
          </div>
        </form>
      </footer>
    </aside>
  );
}
