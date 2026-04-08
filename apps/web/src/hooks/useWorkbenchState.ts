import { ChangeEvent, FormEvent, startTransition, useCallback, useEffect, useRef, useState } from "react";
import {
  approveTaskPlan,
  createMessageStream,
  createSession,
  getFilePreview,
  getSessionMessages,
  getSessionTasks,
  getTask,
  getTaskEvents,
  patchTaskPlanDraft,
  rejectTaskPlan,
  rerunTask,
  uploadFile,
} from "../api";
import type {
  Artifact,
  OperationPlan,
  SessionMessage,
  TaskDetail,
  TaskSpec,
  TimeRange,
  MessageUnderstanding,
  ResponseMode,
  UploadResponse,
  UnderstandingResponsePayload,
} from "../types";
import { useTaskEventStream } from "./useTaskEventStream";

const TERMINAL_STATUSES = new Set(["success", "failed", "waiting_clarification", "cancelled"]);
const SESSION_STORAGE_KEY = "gis-agent/session-id";
const THEME_STORAGE_KEY = "gis-agent/theme";

const RASTER_EXTENSIONS = new Set(["tif", "tiff", "img", "vrt", "jp2", "asc", "bil"]);
const VECTOR_EXTENSIONS = new Set(["geojson", "json", "shp", "gpkg", "kml", "kmz", "zip"]);
const GIS_OPERATION_HINT_PATTERN =
  /(?:分析|计算|裁剪|缓冲|重采样|重投影|叠加|提取|导出|统计|workflow|ndvi|ndwi|clip|buffer|reproject|resample|band\s*math)/i;
const EXECUTION_CONFIRMATION_HINT_PATTERN = /(?:开始执行|执行吧|继续|确认|批准|run|go|yes|ok)/i;
const UPLOAD_CONTEXT_HINT_PATTERN = /(?:上传|文件|读取|读到|访问|识别|查看|upload|file|read|access)/i;
const UPLOAD_STATUS_FOLLOWUP_HINT_PATTERN = /(?:现在呢|更新了吗|你没更新|移除|删除|还剩|剩下|还有几个|变了吗|还在吗|refresh|updated|now)/i;

let sessionIdPromise: Promise<string> | null = null;

type UnderstandingSnapshot = {
  responseMode: ResponseMode | null;
  understanding: MessageUnderstanding | null;
  responsePayload: UnderstandingResponsePayload | null;
};

export type TaskHistoryEntry = {
  task_id: string;
  parent_task_id?: string | null;
  status: string;
  current_step?: string | null;
  analysis_type: string;
  created_at?: string | null;
  task_spec?: TaskSpec | null;
};

export type ThemeMode = "light" | "dark";
export type LayerKey = "basemap" | "aoi";
export type LocalSourceKind = "raster" | "vector" | "unknown";

export type LayerControlState = {
  visible: boolean;
  opacity: number;
};

export type LayerControlMap = Record<LayerKey, LayerControlState>;

export type UploadedLayerPreview = {
  kind: LocalSourceKind;
  rasterPreviewImageUrl?: string;
  geojsonText?: string;
  previewBounds?: [number, number, number, number];
  originalName: string;
};

export type UploadPreviewStatus = "pending" | "ready" | "unsupported" | "failed";

export type MapFocusRequest = {
  kind: "upload" | "artifact";
  id: string;
  nonce: number;
};

export const DEFAULT_LAYER_CONTROLS: LayerControlMap = {
  basemap: { visible: true, opacity: 1 },
  aoi: { visible: true, opacity: 1 },
};

function normalizeExtension(fileName: string): string {
  const index = fileName.lastIndexOf(".");
  if (index < 0 || index >= fileName.length - 1) {
    return "";
  }
  return fileName.slice(index + 1).toLowerCase();
}

export function getUploadSourceKind(upload: UploadResponse): LocalSourceKind {
  const ext = normalizeExtension(upload.original_name);
  if (RASTER_EXTENSIONS.has(ext)) {
    return "raster";
  }
  if (VECTOR_EXTENSIONS.has(ext)) {
    return "vector";
  }

  const lowerType = upload.file_type.toLowerCase();
  if (RASTER_EXTENSIONS.has(lowerType)) {
    return "raster";
  }
  if (VECTOR_EXTENSIONS.has(lowerType)) {
    return "vector";
  }
  return "unknown";
}

export function isRasterArtifact(artifact: Artifact): boolean {
  if (artifact.artifact_type === "geotiff") {
    return true;
  }
  return artifact.mime_type.includes("tiff");
}

export function isGeojsonArtifact(artifact: Artifact): boolean {
  if (artifact.artifact_type === "geojson" || artifact.artifact_type === "vector_geojson") {
    return true;
  }
  return artifact.mime_type.includes("geo+json") || artifact.mime_type.includes("application/json");
}

export function isMapRenderableArtifact(artifact: Artifact): boolean {
  return isRasterArtifact(artifact) || isGeojsonArtifact(artifact);
}

function toTaskHistoryEntry(task: TaskDetail): TaskHistoryEntry {
  return {
    task_id: task.task_id,
    parent_task_id: task.parent_task_id,
    status: task.status,
    current_step: task.current_step,
    analysis_type: task.analysis_type,
    created_at: task.created_at,
    task_spec: task.task_spec,
  };
}

function toTaskHistoryEntryFromSessionTask(task: {
  task_id: string;
  parent_task_id?: string | null;
  status: string;
  current_step?: string | null;
  analysis_type: string;
  created_at?: string | null;
  task_spec?: TaskSpec | null;
}): TaskHistoryEntry {
  return {
    task_id: task.task_id,
    parent_task_id: task.parent_task_id,
    status: task.status,
    current_step: task.current_step,
    analysis_type: task.analysis_type,
    created_at: task.created_at,
    task_spec: task.task_spec,
  };
}

function upsertTaskHistory(current: TaskHistoryEntry[], task: TaskDetail): TaskHistoryEntry[] {
  const nextEntry = toTaskHistoryEntry(task);
  const remaining = current.filter((item) => item.task_id !== task.task_id);
  return [nextEntry, ...remaining].slice(0, 12);
}

function mergeSessionMessages(current: SessionMessage[], incoming: SessionMessage[], prepend: boolean): SessionMessage[] {
  const merged = prepend ? [...incoming, ...current] : [...current, ...incoming];
  const dedup = new Map<string, SessionMessage>();
  for (const message of merged) {
    if (!dedup.has(message.message_id)) {
      dedup.set(message.message_id, message);
    }
  }
  return [...dedup.values()];
}

async function ensureSessionId(): Promise<string> {
  if (!sessionIdPromise) {
    sessionIdPromise = createSession().then((session) => {
      window.localStorage.setItem(SESSION_STORAGE_KEY, session.session_id);
      return session.session_id;
    });
  }
  return sessionIdPromise;
}

export function toggleTheme(theme: ThemeMode): ThemeMode {
  return theme === "dark" ? "light" : "dark";
}

type StreamMode = "streaming" | "fallback_polling";

function buildUnderstandingSnapshot(
  response: Pick<UnderstandingSnapshot, "responseMode" | "understanding" | "responsePayload">,
): UnderstandingSnapshot {
  return {
    responseMode: response.responseMode ?? null,
    understanding: response.understanding ?? null,
    responsePayload: response.responsePayload ?? null,
  };
}

function summarizeUnderstanding(understanding: MessageUnderstanding | null | undefined, fallback?: string | null): string | null {
  if (understanding?.understanding_summary) {
    return understanding.understanding_summary;
  }
  if (fallback) {
    return fallback;
  }
  return null;
}

function formatMissingFields(fields: string[]): string {
  if (!fields.length) {
    return "当前还缺少关键信息。";
  }
  return `当前还缺少这些关键信息：${fields.join("、")}。`;
}

function buildResponseModeNotice(response: {
  response_mode?: ResponseMode | null;
  understanding?: MessageUnderstanding | null;
  response_payload?: UnderstandingResponsePayload | null;
  task_status?: string | null;
  need_clarification?: boolean;
  awaiting_task_confirmation?: boolean;
  need_approval?: boolean;
  missing_fields?: string[];
  clarification_message?: string | null;
  assistant_message?: string | null;
}): string | null {
  const responseMode = response.response_mode ?? response.response_payload?.response_mode ?? null;
  const payload = response.response_payload;
  const understandingSummary = summarizeUnderstanding(response.understanding ?? null, payload?.understanding_summary ?? null);
  const missingFields = payload?.missing_fields ?? response.missing_fields ?? [];
  const blockedReason = payload?.blocked_reason ?? null;
  const executionBlocked = Boolean(payload?.execution_blocked);

  switch (responseMode) {
    case "confirm_understanding":
      return understandingSummary ? `我理解的是：${understandingSummary}。如果没问题，请继续。` : "我已经理解当前需求，等待你确认。";
    case "ask_missing_fields":
      if (executionBlocked) {
        return blockedReason
          ? `当前版本已被阻止执行：${blockedReason}。请先修正可编辑字段。`
          : "当前版本已被阻止执行。请先修正可编辑字段。";
      }
      return formatMissingFields(missingFields);
    case "execute_now":
      if (response.task_status === "awaiting_approval" || response.need_approval || payload?.require_approval) {
        return understandingSummary
          ? `我理解的是：${understandingSummary}。计划已生成，等待你批准后执行。`
          : "关键信息已确认，计划已生成，等待你批准后执行。";
      }
      return understandingSummary ? `我理解的是：${understandingSummary}。系统会直接开始执行。` : "关键信息齐了，系统会直接开始执行。";
    case "awaiting_approval":
      return understandingSummary
        ? `我理解的是：${understandingSummary}。计划已进入审批阶段。`
        : "计划已进入审批阶段，请先审阅再执行。";
    case "show_revision":
      return understandingSummary ? `这是当前理解的修订版：${understandingSummary}。` : "这是当前理解的修订版。";
    default:
      if (response.task_status === "awaiting_approval") {
        return "任务已进入审批阶段，请先审阅再执行。";
      }
      if (response.awaiting_task_confirmation) {
        return "请在对话中回复“开始执行”或“继续”，以确认创建任务。";
      }
      if (response.need_clarification) {
        return response.clarification_message ?? "任务需要补充信息后才能继续执行。";
      }
      return response.assistant_message ?? null;
  }
}

export function useWorkbenchState() {
  const [theme, setTheme] = useState<ThemeMode>("light");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);

  const [composerText, setComposerText] = useState("");
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [uploads, setUploads] = useState<UploadResponse[]>([]);
  const [sourceFileId, setSourceFileId] = useState<string | null>(null);
  const [clipFileId, setClipFileId] = useState<string | null>(null);
  const [uploadedLayerPreviews, setUploadedLayerPreviews] = useState<Record<string, UploadedLayerPreview>>({});
  const [uploadPreviewStatuses, setUploadPreviewStatuses] = useState<Record<string, UploadPreviewStatus>>({});
  const [mapFocusRequest, setMapFocusRequest] = useState<MapFocusRequest | null>(null);
  const [uploadLayerControls, setUploadLayerControls] = useState<Record<string, LayerControlState>>({});
  const [artifactLayerControls, setArtifactLayerControls] = useState<Record<string, LayerControlState>>({});

  const [isUploading, setIsUploading] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [activeActionId, setActiveActionId] = useState<string | null>(null);

  const [taskId, setTaskId] = useState<string | null>(null);
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [taskEventsCount, setTaskEventsCount] = useState(0);
  const [taskHistory, setTaskHistory] = useState<TaskHistoryEntry[]>([]);
  const [latestUnderstandingSnapshot, setLatestUnderstandingSnapshot] = useState<UnderstandingSnapshot>({
    responseMode: null,
    understanding: null,
    responsePayload: null,
  });

  const [layerControls, setLayerControls] = useState<LayerControlMap>(DEFAULT_LAYER_CONTROLS);

  const [planDraftText, setPlanDraftText] = useState("");
  const [planError, setPlanError] = useState<string | null>(null);
  const [isSavingPlan, setIsSavingPlan] = useState(false);
  const [isApprovingPlan, setIsApprovingPlan] = useState(false);
  const [isRejectingPlan, setIsRejectingPlan] = useState(false);

  const [streamMode, setStreamMode] = useState<StreamMode>("streaming");
  const [streamCursor, setStreamCursor] = useState(0);

  const [messages, setMessages] = useState<SessionMessage[]>([]);
  const [messagesNextCursor, setMessagesNextCursor] = useState<string | null>(null);
  const [isLoadingMessages, setIsLoadingMessages] = useState(false);
  const [messagesError, setMessagesError] = useState<string | null>(null);
  const [streamingAssistantMessage, setStreamingAssistantMessage] = useState<string | null>(null);

  const activeTaskIdRef = useRef<string | null>(null);
  const refreshInFlightRef = useRef(false);
  const refreshQueuedRef = useRef(false);
  const streamCursorRef = useRef(0);
  const mapFocusNonceRef = useRef(0);

  const pushMapFocusRequest = useCallback((kind: MapFocusRequest["kind"], id: string): void => {
    mapFocusNonceRef.current += 1;
    setMapFocusRequest({ kind, id, nonce: mapFocusNonceRef.current });
  }, []);

  useEffect(() => {
    activeTaskIdRef.current = taskId;
  }, [taskId]);

  useEffect(() => {
    streamCursorRef.current = streamCursor;
  }, [streamCursor]);

  const loadLatestMessages = useCallback(async (targetSessionId: string): Promise<void> => {
    setIsLoadingMessages(true);
    setMessagesError(null);
    try {
      const response = await getSessionMessages(targetSessionId, 30);
      startTransition(() => {
        setMessages(response.messages);
        setMessagesNextCursor(response.next_cursor ?? null);
      });
    } catch (error) {
      setMessagesError(error instanceof Error ? error.message : "消息历史加载失败。");
    } finally {
      setIsLoadingMessages(false);
    }
  }, []);

  const loadMoreMessages = useCallback(async (): Promise<void> => {
    if (!sessionId || !messagesNextCursor || isLoadingMessages) {
      return;
    }
    setIsLoadingMessages(true);
    setMessagesError(null);
    try {
      const response = await getSessionMessages(sessionId, 30, messagesNextCursor);
      setMessages((current) => mergeSessionMessages(current, response.messages, true));
      setMessagesNextCursor(response.next_cursor ?? null);
    } catch (error) {
      setMessagesError(error instanceof Error ? error.message : "加载更早消息失败。");
    } finally {
      setIsLoadingMessages(false);
    }
  }, [isLoadingMessages, messagesNextCursor, sessionId]);

  const refreshTaskState = useCallback(async (targetTaskId?: string | null): Promise<void> => {
    const nextTaskId = targetTaskId ?? activeTaskIdRef.current;
    if (!nextTaskId) {
      return;
    }
    if (refreshInFlightRef.current) {
      refreshQueuedRef.current = true;
      return;
    }

    refreshInFlightRef.current = true;
    try {
      const [detail, eventsResponse] = await Promise.all([
        getTask(nextTaskId),
        getTaskEvents(nextTaskId, streamCursorRef.current),
      ]);
      if (activeTaskIdRef.current !== nextTaskId) {
        return;
      }
      startTransition(() => {
        setTask(detail);
        if (eventsResponse.events.length > 0) {
          setTaskEventsCount((current) => current + eventsResponse.events.length);
        }
        setTaskHistory((current) => upsertTaskHistory(current, detail));
        setStreamCursor(eventsResponse.next_cursor);
      });
      streamCursorRef.current = eventsResponse.next_cursor;
    } catch (error) {
      if (activeTaskIdRef.current === nextTaskId) {
        setSubmitError(error instanceof Error ? error.message : "任务状态获取失败。");
      }
    } finally {
      refreshInFlightRef.current = false;
      if (refreshQueuedRef.current) {
        refreshQueuedRef.current = false;
        void refreshTaskState(nextTaskId);
      }
    }
  }, []);

  const hydrateTask = useCallback(async (nextTaskId: string): Promise<void> => {
    const [detail, eventsResponse] = await Promise.all([getTask(nextTaskId), getTaskEvents(nextTaskId)]);
    startTransition(() => {
      setTaskId(nextTaskId);
      setTask(detail);
      setTaskEventsCount(eventsResponse.events.length);
      setTaskHistory((current) => upsertTaskHistory(current, detail));
      setStreamMode("streaming");
      setStreamCursor(eventsResponse.next_cursor);
    });
  }, []);

  useTaskEventStream({
    taskId,
    sinceId: streamCursor,
    enabled: Boolean(taskId && streamMode === "streaming" && task && !TERMINAL_STATUSES.has(task.status)),
    onEvent: (event) => {
      if (event.event_id <= streamCursorRef.current) {
        return;
      }
      const nextCursor = Math.max(streamCursorRef.current, event.event_id);
      setStreamCursor(nextCursor);
      streamCursorRef.current = nextCursor;
      setTaskEventsCount((current) => current + 1);
      void refreshTaskState(taskId);
    },
    onError: () => {
      setStreamMode("fallback_polling");
    },
  });

  useEffect(() => {
    if (!taskId || streamMode !== "fallback_polling" || (task && TERMINAL_STATUSES.has(task.status))) {
      return;
    }
    const interval = window.setInterval(() => {
      void refreshTaskState(taskId);
    }, 3000);
    return () => {
      window.clearInterval(interval);
    };
  }, [refreshTaskState, streamMode, task, taskId]);

  useEffect(() => {
    const storedTheme = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (storedTheme === "light" || storedTheme === "dark") {
      setTheme(storedTheme);
    }
  }, []);

  useEffect(() => {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    let cancelled = false;

    void (async () => {
      try {
        const storedSessionId = window.localStorage.getItem(SESSION_STORAGE_KEY);
        if (storedSessionId) {
          try {
            const response = await getSessionTasks(storedSessionId, 12);
            if (!cancelled) {
              setSessionId(storedSessionId);
              setTaskHistory(response.tasks.map(toTaskHistoryEntryFromSessionTask));
              void loadLatestMessages(storedSessionId);
            }
            if (!cancelled && response.tasks[0]) {
              await hydrateTask(response.tasks[0].task_id);
              return;
            }
            if (!cancelled) {
              return;
            }
          } catch {
            window.localStorage.removeItem(SESSION_STORAGE_KEY);
            sessionIdPromise = null;
          }
        }

        const nextSessionId = await ensureSessionId();
        if (!cancelled) {
          setSessionId(nextSessionId);
          void loadLatestMessages(nextSessionId);
        }
      } catch (error) {
        if (!cancelled) {
          setBootError(error instanceof Error ? error.message : "Session 初始化失败。");
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [hydrateTask, loadLatestMessages]);

  useEffect(() => {
    if (task?.aoi_bbox_bounds) {
      setLayerControls((current) => ({
        ...current,
        aoi: { ...current.aoi, visible: true },
      }));
    }
  }, [task?.aoi_bbox_bounds, task?.task_id]);

  useEffect(() => {
    const planText = task?.operation_plan ? JSON.stringify(task.operation_plan, null, 2) : "";
    setPlanDraftText((current) => (current === planText ? current : planText));
    setPlanError(null);
  }, [task?.task_id, task?.operation_plan?.version]);

  useEffect(() => {
    const artifacts = task?.artifacts ?? [];
    setArtifactLayerControls((current) => {
      const next: Record<string, LayerControlState> = {};
      for (const artifact of artifacts) {
        if (!isMapRenderableArtifact(artifact)) {
          continue;
        }
        next[artifact.artifact_id] =
          current[artifact.artifact_id] ?? {
            visible: true,
            opacity: isRasterArtifact(artifact) ? 0.82 : 1,
          };
      }
      return next;
    });
  }, [task?.artifacts, task?.task_id]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!sessionId) {
      setBootError("Session 尚未初始化完成。");
      return;
    }

    const normalizedContent = composerText.trim();
    if (!normalizedContent) {
      return;
    }

    const shouldAttachFileIds =
      GIS_OPERATION_HINT_PATTERN.test(normalizedContent) ||
      EXECUTION_CONFIRMATION_HINT_PATTERN.test(normalizedContent) ||
      UPLOAD_CONTEXT_HINT_PATTERN.test(normalizedContent) ||
      (uploads.length > 0 && UPLOAD_STATUS_FOLLOWUP_HINT_PATTERN.test(normalizedContent));
    const fileIdsForSubmit = shouldAttachFileIds
      ? uploads.map((item) => item.file_id)
      : [];

    const optimisticUserMessage: SessionMessage = {
      message_id: `local_user_${Date.now()}`,
      role: "user",
      content: normalizedContent,
      linked_task_id: null,
      created_at: new Date().toISOString(),
    };
    setMessages((current) => mergeSessionMessages(current, [optimisticUserMessage], false));
    setComposerText("");

    setStreamingAssistantMessage(null);
    setIsSubmitting(true);
    setSubmitError(null);
    setNotice(null);

    try {
      let hasDelta = false;
      let streamedAssistant = "";
      const response = await createMessageStream(
        {
          session_id: sessionId,
          content: normalizedContent,
          file_ids: fileIdsForSubmit,
        },
        {
          onDelta: (text) => {
            hasDelta = true;
            streamedAssistant += text;
            setStreamingAssistantMessage((current) => `${current ?? ""}${text}`);
          },
        },
      );

      const nextUnderstandingSnapshot = buildUnderstandingSnapshot({
        responseMode: response.response_mode ?? null,
        understanding: response.understanding ?? null,
        responsePayload: response.response_payload ?? null,
      });
      setLatestUnderstandingSnapshot(nextUnderstandingSnapshot);

      const responseNotice = buildResponseModeNotice(response);
      if (responseNotice) {
        setNotice(responseNotice);
      }

      if (response.mode === "task" && response.task_id) {
        setStreamingAssistantMessage(null);
        await Promise.all([hydrateTask(response.task_id), loadLatestMessages(sessionId)]);
      } else {
        const finalAssistantText = streamedAssistant || response.assistant_message || "";
        if (finalAssistantText) {
          if (!hasDelta) {
            setStreamingAssistantMessage(finalAssistantText);
          }
          const optimisticAssistantMessage: SessionMessage = {
            message_id: `local_assistant_${Date.now()}`,
            role: "assistant",
            content: finalAssistantText,
            linked_task_id: null,
            created_at: new Date().toISOString(),
          };
          setMessages((current) => mergeSessionMessages(current, [optimisticAssistantMessage], false));
        }

        setStreamingAssistantMessage(null);

        void loadLatestMessages(sessionId);
      }
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : "任务提交失败。");
      setStreamingAssistantMessage(null);
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>): Promise<void> {
    const selectedFiles = event.target.files;
    if (!selectedFiles?.length || !sessionId) {
      return;
    }

    setUploadError(null);
    setNotice(null);
    setIsUploading(true);

    const files = Array.from(selectedFiles);
    let uploadedCount = 0;
    let hasRasterAssigned = Boolean(sourceFileId);
    let hasVectorAssigned = Boolean(clipFileId);

    try {
      for (const file of files) {
        const response = await uploadFile(sessionId, file);
        const sourceKind = getUploadSourceKind(response);

        let preview: UploadedLayerPreview = {
          kind: sourceKind,
          originalName: response.original_name,
        };

        const fileExt = normalizeExtension(response.original_name);
        if (sourceKind === "vector" && (fileExt === "geojson" || fileExt === "json")) {
          try {
            preview = {
              ...preview,
              geojsonText: await file.text(),
            };
          } catch {
            // Keep vector layer in list even if local preview parsing fails.
          }
        }

        setUploads((current) => [...current, response]);
        setUploadedLayerPreviews((current) => ({
          ...current,
          [response.file_id]: preview,
        }));
        setUploadLayerControls((current) => ({
          ...current,
          [response.file_id]: {
            visible: true,
            opacity: sourceKind === "raster" ? 0.82 : 1,
          },
        }));
        setUploadPreviewStatuses((current) => ({
          ...current,
          [response.file_id]: "pending",
        }));

        try {
          const serverPreview = await getFilePreview(response.file_id);
          const hasBounds =
            Array.isArray(serverPreview.bbox_bounds) &&
            serverPreview.bbox_bounds.length === 4 &&
            serverPreview.bbox_bounds.every((value) => typeof value === "number" && Number.isFinite(value));
          const previewBounds = hasBounds
            ? (serverPreview.bbox_bounds as [number, number, number, number])
            : undefined;
          let previewStatus: UploadPreviewStatus = "unsupported";

          if (serverPreview.preview_type === "vector_geojson" && serverPreview.geojson) {
            preview = {
              ...preview,
              geojsonText: JSON.stringify(serverPreview.geojson),
              previewBounds,
            };
            previewStatus = "ready";
          }

          if (serverPreview.preview_type === "raster_image" && serverPreview.image_url && previewBounds) {
            preview = {
              ...preview,
              rasterPreviewImageUrl: serverPreview.image_url,
              previewBounds,
            };
            previewStatus = "ready";
          }

          setUploadedLayerPreviews((current) => ({
            ...current,
            [response.file_id]: preview,
          }));
          setUploadPreviewStatuses((current) => ({
            ...current,
            [response.file_id]: previewStatus,
          }));
          if (previewStatus === "ready") {
            pushMapFocusRequest("upload", response.file_id);
          }

          if (serverPreview.message) {
            setNotice(serverPreview.message);
          }
        } catch {
          const hasFallbackPreview = Boolean(
            (preview.kind === "vector" && preview.geojsonText) ||
              (preview.kind === "raster" && preview.rasterPreviewImageUrl && preview.previewBounds),
          );
          setUploadPreviewStatuses((current) => ({
            ...current,
            [response.file_id]: hasFallbackPreview ? "ready" : "failed",
          }));
          if (hasFallbackPreview) {
            pushMapFocusRequest("upload", response.file_id);
          }
        }

        if (!hasRasterAssigned && sourceKind === "raster") {
          setSourceFileId(response.file_id);
          hasRasterAssigned = true;
        }
        if (sourceKind === "vector" && !hasVectorAssigned) {
          setClipFileId(response.file_id);
          hasVectorAssigned = true;
        }

        uploadedCount += 1;
      }

      if (uploadedCount > 0) {
        setNotice(uploadedCount === 1 ? `已加载本地文件 ${files[0].name}。` : `已加载 ${uploadedCount} 个本地文件。`);
      }
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "文件上传失败。");
    } finally {
      setIsUploading(false);
      event.target.value = "";
    }
  }

  function removeUploadedFile(fileId: string): void {
    setUploads((current) => current.filter((item) => item.file_id !== fileId));
    setUploadedLayerPreviews((current) => {
      const next = { ...current };
      delete next[fileId];
      return next;
    });
    setUploadLayerControls((current) => {
      const next = { ...current };
      delete next[fileId];
      return next;
    });
    setUploadPreviewStatuses((current) => {
      const next = { ...current };
      delete next[fileId];
      return next;
    });
    setSourceFileId((current) => (current === fileId ? null : current));
    setClipFileId((current) => (current === fileId ? null : current));
  }

  async function handleRerun(actionId: string, override: Record<string, unknown>): Promise<void> {
    if (!task) {
      return;
    }

    setActiveActionId(actionId);
    setSubmitError(null);
    setNotice(null);

    try {
      const rerunDetail = await rerunTask(task.task_id, override);
      startTransition(() => {
        setTaskId(rerunDetail.task_id);
        setTask(rerunDetail);
        setTaskHistory((current) => upsertTaskHistory(current, rerunDetail));
        setTaskEventsCount(rerunDetail.steps.length);
        setStreamMode("streaming");
        setStreamCursor(0);
      });
      setNotice(`已基于 ${task.task_id} 创建重跑任务 ${rerunDetail.task_id}。`);
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : "任务重跑失败。");
    } finally {
      setActiveActionId(null);
    }
  }

  function handleHistorySelect(nextTaskId: string): void {
    setNotice(null);
    setSubmitError(null);
    void hydrateTask(nextTaskId);
  }

  function updateLayerControl(layer: LayerKey, nextValue: Partial<LayerControlState>): void {
    setLayerControls((current) => ({
      ...current,
      [layer]: { ...current[layer], ...nextValue },
    }));
  }

  function updateUploadLayerControl(fileId: string, nextValue: Partial<LayerControlState>): void {
    setUploadLayerControls((current) => {
      const baseline = current[fileId] ?? { visible: true, opacity: 1 };
      return {
        ...current,
        [fileId]: { ...baseline, ...nextValue },
      };
    });
  }

  function updateArtifactLayerControl(artifactId: string, nextValue: Partial<LayerControlState>): void {
    setArtifactLayerControls((current) => {
      const baseline = current[artifactId] ?? { visible: true, opacity: 1 };
      return {
        ...current,
        [artifactId]: { ...baseline, ...nextValue },
      };
    });
  }

  function focusUploadLayer(fileId: string): void {
    pushMapFocusRequest("upload", fileId);
  }

  function focusArtifactLayer(artifactId: string): void {
    pushMapFocusRequest("artifact", artifactId);
  }

  function selectSourceFile(nextFileId: string): void {
    setSourceFileId(nextFileId);
  }

  function selectClipFile(nextFileId: string | null): void {
    setClipFileId(nextFileId);
  }

  async function handleSavePlanDraft(): Promise<void> {
    if (!task?.operation_plan) {
      return;
    }

    setPlanError(null);
    setSubmitError(null);
    setNotice(null);
    setIsSavingPlan(true);

    try {
      const parsedPlan = JSON.parse(planDraftText) as OperationPlan;
      const detail = await patchTaskPlanDraft(task.task_id, parsedPlan);
      startTransition(() => {
        setTask(detail);
        setTaskHistory((current) => upsertTaskHistory(current, detail));
      });
      setNotice(`计划草稿已保存（v${detail.operation_plan?.version ?? parsedPlan.version}）。`);
    } catch (error) {
      setPlanError(error instanceof Error ? error.message : "计划草稿保存失败。");
    } finally {
      setIsSavingPlan(false);
    }
  }

  async function handleApprovePlan(): Promise<void> {
    if (!task?.operation_plan) {
      return;
    }

    setPlanError(null);
    setSubmitError(null);
    setNotice(null);
    setIsApprovingPlan(true);

    try {
      const detail = await approveTaskPlan(task.task_id, task.operation_plan.version);
      startTransition(() => {
        setTask(detail);
        setTaskHistory((current) => upsertTaskHistory(current, detail));
      });
      setNotice(`计划 v${task.operation_plan.version} 已确认，任务进入执行链路。`);
    } catch (error) {
      setPlanError(error instanceof Error ? error.message : "计划审批失败。");
    } finally {
      setIsApprovingPlan(false);
    }
  }

  async function handleRejectPlan(reason?: string): Promise<void> {
    if (!task) {
      return;
    }

    setPlanError(null);
    setSubmitError(null);
    setNotice(null);
    setIsRejectingPlan(true);

    try {
      const detail = await rejectTaskPlan(task.task_id, reason);
      startTransition(() => {
        setTask(detail);
        setTaskHistory((current) => upsertTaskHistory(current, detail));
      });
      setNotice("当前计划已拒绝，任务已取消。你可以继续对话生成新方案。");
    } catch (error) {
      setPlanError(error instanceof Error ? error.message : "计划拒绝失败。");
    } finally {
      setIsRejectingPlan(false);
    }
  }

  const rerunActions = task
    ? [
        {
          id: "rerun-same",
          label: "按原参数重跑",
          description: "复用当前 TaskSpec 与 AOI",
          override: {},
        },
        {
          id: "rerun-june-2023",
          label: "切到 2023-06",
          description: "保留 AOI，仅重算时间窗",
          override: { time_range: { start: "2023-06-01", end: "2023-06-30" } as TimeRange },
        },
        {
          id: "rerun-add-geotiff",
          label: "补 GeoTIFF",
          description: "在现有输出上追加 GeoTIFF",
          override: {
            preferred_output: Array.from(
              new Set([...(task.task_spec?.preferred_output ?? ["png_map", "methods_text"]), "geotiff"]),
            ),
          },
        },
      ]
    : [];

  return {
    theme,
    setTheme,
    sessionId,
    bootError,
    composerText,
    setComposerText,
    submitError,
    uploadError,
    notice,
    uploads,
    sourceFileId,
    clipFileId,
    uploadedLayerPreviews,
    uploadPreviewStatuses,
    mapFocusRequest,
    uploadLayerControls,
    artifactLayerControls,
    isUploading,
    isSubmitting,
    activeActionId,
    taskId,
    task,
    taskEventsCount,
    taskHistory,
    latestUnderstandingSnapshot,
    layerControls,
    planDraftText,
    setPlanDraftText,
    planError,
    isSavingPlan,
    isApprovingPlan,
    isRejectingPlan,
    rerunActions,
    streamMode,
    messages,
    streamingAssistantMessage,
    messagesNextCursor,
    isLoadingMessages,
    messagesError,
    handleSubmit,
    handleFileChange,
    handleRerun,
    handleHistorySelect,
    updateLayerControl,
    updateUploadLayerControl,
    updateArtifactLayerControl,
    focusUploadLayer,
    focusArtifactLayer,
    selectSourceFile,
    selectClipFile,
    removeUploadedFile,
    getUploadSourceKind,
    loadMoreMessages,
    handleSavePlanDraft,
    handleApprovePlan,
    handleRejectPlan,
  };
}

export type WorkbenchState = ReturnType<typeof useWorkbenchState>;
