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
  UploadResponse,
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
  const streamCursorRef = useRef(0);
  const assistantStreamTimerRef = useRef<number | null>(null);
  const streamPlaybackIntervalRef = useRef<number | null>(null);
  const streamPlaybackQueueRef = useRef("");
  const streamPlaybackDoneRef = useRef(false);
  const streamPlaybackResolverRef = useRef<(() => void) | null>(null);
  const mapFocusNonceRef = useRef(0);

  const pushMapFocusRequest = useCallback((kind: MapFocusRequest["kind"], id: string): void => {
    mapFocusNonceRef.current += 1;
    setMapFocusRequest({ kind, id, nonce: mapFocusNonceRef.current });
  }, []);

  const clearAssistantStreamTimer = useCallback(() => {
    if (assistantStreamTimerRef.current !== null) {
      window.clearTimeout(assistantStreamTimerRef.current);
      assistantStreamTimerRef.current = null;
    }
  }, []);

  const stopStreamPlayback = useCallback(() => {
    if (streamPlaybackIntervalRef.current !== null) {
      window.clearInterval(streamPlaybackIntervalRef.current);
      streamPlaybackIntervalRef.current = null;
    }
  }, []);

  const resetStreamPlayback = useCallback(() => {
    stopStreamPlayback();
    streamPlaybackQueueRef.current = "";
    streamPlaybackDoneRef.current = false;
    if (streamPlaybackResolverRef.current) {
      streamPlaybackResolverRef.current();
      streamPlaybackResolverRef.current = null;
    }
  }, [stopStreamPlayback]);

  const startStreamPlayback = useCallback((): Promise<void> => {
    resetStreamPlayback();
    setStreamingAssistantMessage("");

    return new Promise<void>((resolve) => {
      streamPlaybackResolverRef.current = resolve;
      streamPlaybackIntervalRef.current = window.setInterval(() => {
        const pending = streamPlaybackQueueRef.current;
        if (pending.length > 0) {
          const take = 1;
          const chunk = pending.slice(0, take);
          streamPlaybackQueueRef.current = pending.slice(take);
          setStreamingAssistantMessage((current) => `${current ?? ""}${chunk}`);
          return;
        }

        if (streamPlaybackDoneRef.current) {
          stopStreamPlayback();
          streamPlaybackResolverRef.current = null;
          resolve();
        }
      }, 24);
    });
  }, [resetStreamPlayback, stopStreamPlayback]);

  const enqueueStreamDelta = useCallback((delta: string) => {
    streamPlaybackQueueRef.current += delta;
  }, []);

  const finishStreamPlayback = useCallback(() => {
    streamPlaybackDoneRef.current = true;
    if (streamPlaybackIntervalRef.current === null && streamPlaybackResolverRef.current) {
      const resolve = streamPlaybackResolverRef.current;
      streamPlaybackResolverRef.current = null;
      resolve();
    }
  }, []);

  useEffect(() => {
    activeTaskIdRef.current = taskId;
  }, [taskId]);

  useEffect(() => {
    streamCursorRef.current = streamCursor;
  }, [streamCursor]);

  useEffect(() => {
    return () => {
      clearAssistantStreamTimer();
      resetStreamPlayback();
    };
  }, [clearAssistantStreamTimer, resetStreamPlayback]);

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
    if (!nextTaskId || refreshInFlightRef.current) {
      return;
    }

    refreshInFlightRef.current = true;
    try {
      const [detail, eventsResponse] = await Promise.all([getTask(nextTaskId), getTaskEvents(nextTaskId)]);
      if (activeTaskIdRef.current !== nextTaskId) {
        return;
      }
      startTransition(() => {
        setTask(detail);
        setTaskEventsCount(eventsResponse.events.length);
        setTaskHistory((current) => upsertTaskHistory(current, detail));
        setStreamCursor(eventsResponse.next_cursor);
      });
    } catch (error) {
      if (activeTaskIdRef.current === nextTaskId) {
        setSubmitError(error instanceof Error ? error.message : "任务状态获取失败。");
      }
    } finally {
      refreshInFlightRef.current = false;
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
      setStreamCursor(event.event_id);
      streamCursorRef.current = event.event_id;
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

    clearAssistantStreamTimer();
    resetStreamPlayback();
    setStreamingAssistantMessage(null);
    setIsSubmitting(true);
    setSubmitError(null);
    setNotice(null);

    try {
      let streamPlaybackPromise: Promise<void> | null = null;
      let streamPlaybackActive = false;
      let streamedAssistant = "";
      const response = await createMessageStream(
        {
          session_id: sessionId,
          content: normalizedContent,
          file_ids: fileIdsForSubmit,
        },
        {
          onDelta: (text) => {
            streamedAssistant += text;
            if (!streamPlaybackActive) {
              streamPlaybackPromise = startStreamPlayback();
              streamPlaybackActive = true;
            }
            enqueueStreamDelta(text);
          },
        },
      );

      if (response.mode === "task" && response.task_id) {
        if (streamPlaybackActive) {
          finishStreamPlayback();
          await streamPlaybackPromise;
        }
        setStreamingAssistantMessage(null);
        await Promise.all([hydrateTask(response.task_id), loadLatestMessages(sessionId)]);
        if (response.need_clarification && !response.assistant_message) {
          setNotice(response.clarification_message ?? "任务需要补充信息后才能继续执行。");
        }
      } else {
        if (!streamPlaybackActive && response.assistant_message) {
          streamedAssistant = response.assistant_message;
          streamPlaybackPromise = startStreamPlayback();
          streamPlaybackActive = true;
          enqueueStreamDelta(response.assistant_message);
        }

        if (streamPlaybackActive) {
          finishStreamPlayback();
          await streamPlaybackPromise;
        }

        const finalAssistantText = streamedAssistant || response.assistant_message || "";
        if (finalAssistantText) {
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

        if (response.awaiting_task_confirmation) {
          setNotice("请在对话中回复“开始执行”或“继续”，以确认创建任务。");
        }
      }
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : "任务提交失败。");
      resetStreamPlayback();
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
