import { ChangeEvent, FormEvent, startTransition, useCallback, useEffect, useRef, useState } from "react";
import {
  approveTaskPlan,
  createMessage,
  createSession,
  getSessionTasks,
  getTask,
  getTaskEvents,
  patchTaskPlanDraft,
  rerunTask,
  uploadFile,
} from "../api";
import type {
  OperationPlan,
  TaskDetail,
  TaskSpec,
  TimeRange,
  UploadResponse,
} from "../types";
import { useTaskEventStream } from "./useTaskEventStream";

const TERMINAL_STATUSES = new Set(["success", "failed", "waiting_clarification"]);
const SESSION_STORAGE_KEY = "gis-agent/session-id";
const THEME_STORAGE_KEY = "gis-agent/theme";

let sessionIdPromise: Promise<string> | null = null;

export const QUICK_PROMPTS = [
  "帮我算北京西山 2024 年夏季的 NDVI，并导出 GeoTIFF",
  "bbox(116.1,39.8,116.5,40.1) 在 2024-06-01 到 2024-06-30 的 NDVI",
  "上传边界后，计算 2023 年 7 月的 NDVI，并给出方法说明",
];

export const FOLLOWUP_PROMPTS = ["换成 2023 年 6 月", "改成 Landsat，并重新导出 GeoTIFF", "把时间范围放宽到整个夏季"];

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
export type LayerKey = "basemap" | "aoi" | "ndvi";

export type LayerControlState = {
  visible: boolean;
  opacity: number;
};

export type LayerControlMap = Record<LayerKey, LayerControlState>;

export const DEFAULT_LAYER_CONTROLS: LayerControlMap = {
  basemap: { visible: true, opacity: 1 },
  aoi: { visible: true, opacity: 1 },
  ndvi: { visible: true, opacity: 0.82 },
};

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
  return [nextEntry, ...remaining].slice(0, 8);
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
  const [composerText, setComposerText] = useState(QUICK_PROMPTS[0]);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [uploads, setUploads] = useState<UploadResponse[]>([]);
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
  const [streamMode, setStreamMode] = useState<StreamMode>("streaming");
  const [streamCursor, setStreamCursor] = useState(0);

  const activeTaskIdRef = useRef<string | null>(null);
  const refreshInFlightRef = useRef(false);
  const streamCursorRef = useRef(0);

  useEffect(() => {
    activeTaskIdRef.current = taskId;
  }, [taskId]);

  useEffect(() => {
    streamCursorRef.current = streamCursor;
  }, [streamCursor]);

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
            const response = await getSessionTasks(storedSessionId, 8);
            if (!cancelled) {
              setSessionId(storedSessionId);
              setTaskHistory(response.tasks.map(toTaskHistoryEntryFromSessionTask));
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
  }, [hydrateTask]);

  useEffect(() => {
    if (task?.aoi_bbox_bounds) {
      setLayerControls((current) => ({
        ...current,
        aoi: { ...current.aoi, visible: true },
      }));
    }
    if (task?.artifacts.some((artifact) => artifact.artifact_type === "geotiff")) {
      setLayerControls((current) => ({
        ...current,
        ndvi: { ...current.ndvi, visible: true },
      }));
    }
  }, [task?.aoi_bbox_bounds, task?.artifacts, task?.task_id]);

  useEffect(() => {
    const planText = task?.operation_plan ? JSON.stringify(task.operation_plan, null, 2) : "";
    setPlanDraftText((current) => (current === planText ? current : planText));
    setPlanError(null);
  }, [task?.task_id, task?.operation_plan?.version]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!sessionId) {
      setBootError("Session 尚未初始化完成。");
      return;
    }

    setIsSubmitting(true);
    setSubmitError(null);
    setNotice(null);

    try {
      const response = await createMessage({
        session_id: sessionId,
        content: composerText,
        file_ids: uploads.map((item) => item.file_id),
      });
      await hydrateTask(response.task_id);
      setUploads([]);
      setNotice(
        response.need_clarification
          ? response.clarification_message ?? "任务需要补充 AOI 或时间窗。"
          : `任务 ${response.task_id} 已创建，当前状态：${response.task_status}。`,
      );
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : "任务提交失败。");
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>): Promise<void> {
    const file = event.target.files?.[0];
    if (!file || !sessionId) {
      return;
    }

    setUploadError(null);
    setNotice(null);
    setIsUploading(true);

    try {
      const response = await uploadFile(sessionId, file);
      setUploads((current) => [...current, response]);
      setNotice(`已加载 AOI 文件 ${response.original_name}。`);
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "AOI 上传失败。");
    } finally {
      setIsUploading(false);
      event.target.value = "";
    }
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

  function applyPrompt(prompt: string): void {
    setComposerText(prompt);
  }

  function updateLayerControl(layer: LayerKey, nextValue: Partial<LayerControlState>): void {
    setLayerControls((current) => ({
      ...current,
      [layer]: { ...current[layer], ...nextValue },
    }));
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
          id: "rerun-landsat",
          label: "切到 Landsat",
          description: "仅覆盖数据源",
          override: { requested_dataset: "landsat89" },
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
    quickPrompts: QUICK_PROMPTS,
    followupPrompts: FOLLOWUP_PROMPTS,
    rerunActions,
    streamMode,
    handleSubmit,
    handleFileChange,
    handleRerun,
    handleHistorySelect,
    applyPrompt,
    updateLayerControl,
    handleSavePlanDraft,
    handleApprovePlan,
  };
}

export type WorkbenchState = ReturnType<typeof useWorkbenchState>;
