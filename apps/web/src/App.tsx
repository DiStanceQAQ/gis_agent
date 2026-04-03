import { ChangeEvent, FormEvent, startTransition, useDeferredValue, useEffect, useState } from "react";
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
} from "./api";
import GISMap from "./components/GISMap";
import type {
  Artifact,
  Candidate,
  OperationPlan,
  TaskDetail,
  TaskSpec,
  TaskStep,
  TimeRange,
  UploadResponse,
} from "./types";

const TERMINAL_STATUSES = new Set(["success", "failed", "waiting_clarification"]);
const SESSION_STORAGE_KEY = "gis-agent/session-id";
const THEME_STORAGE_KEY = "gis-agent/theme";
let sessionIdPromise: Promise<string> | null = null;

const QUICK_PROMPTS = [
  "帮我算北京西山 2024 年夏季的 NDVI，并导出 GeoTIFF",
  "bbox(116.1,39.8,116.5,40.1) 在 2024-06-01 到 2024-06-30 的 NDVI",
  "上传边界后，计算 2023 年 7 月的 NDVI，并给出方法说明",
];

const FOLLOWUP_PROMPTS = [
  "换成 2023 年 6 月",
  "改成 Landsat，并重新导出 GeoTIFF",
  "把时间范围放宽到整个夏季",
];

type TaskHistoryEntry = {
  task_id: string;
  parent_task_id?: string | null;
  status: string;
  current_step?: string | null;
  analysis_type: string;
  created_at?: string | null;
  task_spec?: TaskSpec | null;
};

type ThemeMode = "light" | "dark";
type LayerKey = "basemap" | "aoi" | "ndvi";

type LayerControlState = {
  visible: boolean;
  opacity: number;
};

type LayerControlMap = Record<LayerKey, LayerControlState>;

const DEFAULT_LAYER_CONTROLS: LayerControlMap = {
  basemap: { visible: true, opacity: 1 },
  aoi: { visible: true, opacity: 1 },
  ndvi: { visible: true, opacity: 0.82 },
};

function formatStatusLabel(status?: string | null): string {
  switch (status) {
    case "queued":
      return "排队中";
    case "running":
      return "执行中";
    case "success":
      return "已完成";
    case "failed":
      return "失败";
    case "waiting_clarification":
      return "等待澄清";
    default:
      return "待开始";
  }
}

function formatStatusTone(status?: string | null): string {
  switch (status) {
    case "success":
      return "success";
    case "failed":
      return "failed";
    case "running":
      return "running";
    case "queued":
      return "queued";
    case "waiting_clarification":
      return "warning";
    default:
      return "idle";
  }
}

function formatDatasetName(dataset?: string | null): string {
  switch (dataset) {
    case "sentinel2":
    case "sentinel-2-l2a":
      return "Sentinel-2 L2A";
    case "landsat89":
    case "landsat-c2-l2":
      return "Landsat 8/9";
    case "hls":
      return "HLS";
    default:
      return dataset ?? "未指定";
  }
}

function formatTimeRange(range?: TimeRange | null): string {
  if (!range?.start || !range?.end) {
    return "未指定";
  }
  return `${range.start} -> ${range.end}`;
}

function formatPercent(value?: number | null): string {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "未提供";
  }
  return `${(value * 100).toFixed(0)}%`;
}

function formatCloudPercent(value?: number | null): string {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "未提供";
  }
  const normalized = value <= 1 ? value * 100 : value;
  return `${normalized.toFixed(0)}%`;
}

function formatDateTime(value?: string | null): string {
  if (!value) {
    return "待更新";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatBytes(sizeBytes: number): string {
  if (sizeBytes >= 1024 * 1024) {
    return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  if (sizeBytes >= 1024) {
    return `${(sizeBytes / 1024).toFixed(1)} KB`;
  }
  return `${sizeBytes} B`;
}

function formatOutputLabel(output: string): string {
  switch (output) {
    case "png_map":
      return "PNG 预览";
    case "geotiff":
      return "GeoTIFF";
    case "methods_text":
      return "方法说明";
    case "summary_text":
      return "结果摘要";
    default:
      return output;
  }
}

function formatSourceLabel(source?: unknown): string {
  switch (source) {
    case "planetary_computer_live":
      return "Planetary Computer";
    case "baseline_catalog":
      return "Baseline Catalog";
    case "sample_fixture":
      return "Sample Fixture";
    default:
      return typeof source === "string" ? source : "未提供";
  }
}

function formatArtifactLabel(artifact: Artifact): string {
  switch (artifact.artifact_type) {
    case "png_map":
      return "PNG 预览";
    case "geotiff":
      return "GeoTIFF";
    case "methods_text":
      return "方法说明";
    case "summary_text":
      return "结果摘要";
    default:
      return artifact.artifact_type;
  }
}

function toDisplayValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "未提供";
  }
  if (typeof value === "boolean") {
    return value ? "是" : "否";
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? value.toString() : value.toFixed(2);
  }
  if (typeof value === "string") {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => toDisplayValue(item)).join(" / ");
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    if (typeof record.start === "string" && typeof record.end === "string") {
      return `${record.start} -> ${record.end}`;
    }
    return JSON.stringify(record);
  }
  return String(value);
}

function getDetailEntries(detail?: Record<string, unknown> | null): Array<[string, string]> {
  if (!detail) {
    return [];
  }

  return Object.entries(detail)
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .slice(0, 5)
    .map(([key, value]) => [key.replace(/_/g, " "), toDisplayValue(value)]);
}

function getOverviewItems(taskSpec?: TaskSpec | null, task?: TaskDetail | null): Array<[string, string]> {
  if (!taskSpec && !task) {
    return [];
  }

  return [
    ["分析类型", task?.analysis_type ?? taskSpec?.analysis_type ?? "NDVI"],
    ["AOI 输入", task?.aoi_name ?? taskSpec?.aoi_input ?? "未提供"],
    ["AOI 来源", taskSpec?.aoi_source_type ?? "未提供"],
    ["请求时间窗", formatTimeRange(task?.requested_time_range ?? taskSpec?.time_range ?? null)],
    ["实际时间窗", formatTimeRange(task?.actual_time_range ?? null)],
    ["指定数据源", formatDatasetName(taskSpec?.requested_dataset)],
    ["优先输出", taskSpec?.preferred_output?.map(formatOutputLabel).join(" / ") ?? "默认"],
    ["创建方式", taskSpec?.created_from ?? "用户首条请求"],
  ];
}

function getPrimaryArtifact(task?: TaskDetail | null, artifactType?: string): Artifact | undefined {
  return task?.artifacts.find((artifact) => artifact.artifact_type === artifactType);
}

function buildPreferredOutputs(task?: TaskDetail | null, extraOutputs?: string[]): string[] {
  const baseOutputs = task?.task_spec?.preferred_output ?? ["png_map", "methods_text"];
  return Array.from(new Set([...baseOutputs, ...(extraOutputs ?? [])]));
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

function toggleTheme(theme: ThemeMode): ThemeMode {
  return theme === "dark" ? "light" : "dark";
}

export default function App() {
  const [theme, setTheme] = useState<ThemeMode>("dark");
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

  const deferredTask = useDeferredValue(task);
  const deferredTaskHistory = useDeferredValue(taskHistory);

  async function hydrateTask(nextTaskId: string): Promise<void> {
    const [detail, eventsResponse] = await Promise.all([getTask(nextTaskId), getTaskEvents(nextTaskId)]);
    startTransition(() => {
      setTaskId(nextTaskId);
      setTask(detail);
      setTaskEventsCount(eventsResponse.events.length);
      setTaskHistory((current) => upsertTaskHistory(current, detail));
    });
  }

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
  }, []);

  useEffect(() => {
    if (!taskId) {
      return;
    }

    let cancelled = false;

    const refreshTaskState = async () => {
      try {
        const [detail, eventsResponse] = await Promise.all([getTask(taskId), getTaskEvents(taskId)]);
        if (cancelled) {
          return;
        }
        startTransition(() => {
          setTask(detail);
          setTaskEventsCount(eventsResponse.events.length);
          setTaskHistory((current) => upsertTaskHistory(current, detail));
        });
      } catch (error) {
        if (cancelled) {
          return;
        }
        setSubmitError(error instanceof Error ? error.message : "任务状态获取失败。");
      }
    };

    void refreshTaskState();

    if (task && TERMINAL_STATUSES.has(task.status)) {
      return () => {
        cancelled = true;
      };
    }

    const interval = window.setInterval(() => {
      void refreshTaskState();
    }, 3000);

    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [task?.status, taskId]);

  useEffect(() => {
    if (task?.aoi_bbox_bounds) {
      setLayerControls((current) => ({
        ...current,
        aoi: { ...current.aoi, visible: true },
      }));
    }
    if (getPrimaryArtifact(task, "geotiff")) {
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

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
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
          : `任务 ${response.task_id} 已创建，当前状态：${formatStatusLabel(response.task_status)}。`,
      );
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : "任务提交失败。");
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
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

  async function handleRerun(actionId: string, override: Record<string, unknown>) {
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
      });
      setNotice(`已基于 ${task.task_id} 创建重跑任务 ${rerunDetail.task_id}。`);
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : "任务重跑失败。");
    } finally {
      setActiveActionId(null);
    }
  }

  function handleHistorySelect(nextTaskId: string) {
    setNotice(null);
    setSubmitError(null);
    void hydrateTask(nextTaskId);
  }

  function applyPrompt(prompt: string) {
    setComposerText(prompt);
  }

  function updateLayerControl(layer: LayerKey, nextValue: Partial<LayerControlState>) {
    setLayerControls((current) => ({
      ...current,
      [layer]: { ...current[layer], ...nextValue },
    }));
  }

  async function handleSavePlanDraft() {
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

  async function handleApprovePlan() {
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

  const currentTask = deferredTask;
  const currentHistory = deferredTaskHistory;
  const overviewItems = getOverviewItems(currentTask?.task_spec, currentTask);
  const pngArtifact = getPrimaryArtifact(currentTask, "png_map");
  const geotiffArtifact = getPrimaryArtifact(currentTask, "geotiff");
  const methodsArtifact = getPrimaryArtifact(currentTask, "methods_text");
  const summaryArtifact = getPrimaryArtifact(currentTask, "summary_text");
  const hasResult = currentTask?.status === "success";
  const exportCards: Array<{ id: string; label: string; subtitle: string; artifact?: Artifact }> = [
    {
      id: "png_map",
      label: "PNG 预览图",
      subtitle: "用于成果快照与汇报展示",
      artifact: pngArtifact,
    },
    {
      id: "geotiff",
      label: "GeoTIFF 栅格",
      subtitle: "用于 GIS 软件继续分析",
      artifact: geotiffArtifact,
    },
    {
      id: "methods_text",
      label: "方法说明",
      subtitle: "记录数据源、处理链路与参数",
      artifact: methodsArtifact,
    },
    {
      id: "summary_text",
      label: "结果摘要",
      subtitle: "汇总指标与结论",
      artifact: summaryArtifact,
    },
  ];
  const rerunActions = currentTask
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
          override: { time_range: { start: "2023-06-01", end: "2023-06-30" } },
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
          override: { preferred_output: buildPreferredOutputs(currentTask, ["geotiff"]) },
        },
      ]
    : [];
  const canEditOperationPlan = currentTask?.status === "awaiting_approval" && Boolean(currentTask.operation_plan);
  const canApproveOperationPlan = canEditOperationPlan && !isSavingPlan && !isApprovingPlan;

  const layerRows = [
    {
      key: "basemap" as LayerKey,
      title: "基础底图",
      subtitle: "OSM 底图",
      available: true,
    },
    {
      key: "aoi" as LayerKey,
      title: "研究区边界",
      subtitle: currentTask?.aoi_name ?? currentTask?.task_spec?.aoi_input ?? "等待 AOI",
      available: Boolean(currentTask?.aoi_bbox_bounds?.length),
    },
    {
      key: "ndvi" as LayerKey,
      title: "NDVI 栅格",
      subtitle: geotiffArtifact ? `${formatBytes(geotiffArtifact.size_bytes)} · GeoTIFF` : "等待 GeoTIFF 产物",
      available: Boolean(geotiffArtifact),
    },
  ];

  return (
    <div className={`app-shell theme-${theme}`}>
      <header className="topbar">
        <div className="topbar-brand">
          <div className="brand-mark" />
          <div>
            <p className="topbar-kicker">GIS Platform</p>
            <h1>GIS Agent Workbench</h1>
          </div>
        </div>

        <div className="topbar-status">
          <div className="topbar-status-row">
            <span className={`status-badge status-${formatStatusTone(currentTask?.status)}`}>
              {formatStatusLabel(currentTask?.status)}
            </span>
            <span className="topbar-chip">Task {currentTask?.task_id ?? "--"}</span>
            <span className="topbar-chip">Session {sessionId ?? "初始化中"}</span>
            <span className="topbar-chip">
              {formatDatasetName(currentTask?.task_spec?.requested_dataset ?? currentTask?.recommendation?.primary_dataset ?? null)}
            </span>
          </div>
          <p className="topbar-status-text">
            {currentTask?.current_step ?? "等待任务输入"} · {currentTask?.aoi_name ?? currentTask?.task_spec?.aoi_input ?? "未设置 AOI"}
          </p>
        </div>

        <div className="topbar-actions">
          <button className="mode-toggle" type="button" onClick={() => setTheme((current) => toggleTheme(current))}>
            {theme === "dark" ? "浅色模式" : "深色模式"}
          </button>
        </div>
      </header>

      <main className="workspace-shell">
        <aside className="sidebar sidebar-left">
          <section className="panel-block result-overview-block">
            <div className="block-header">
              <div>
                <p className="block-kicker">GIS Results</p>
                <h2>结果工作台</h2>
              </div>
              <span className={`panel-pill ${hasResult ? "panel-pill-highlight" : ""}`}>{hasResult ? "结果可用" : "等待执行"}</span>
            </div>

            <div className="result-overview-grid">
              <article className="result-overview-card">
                <span>数据源</span>
                <strong>
                  {formatDatasetName(
                    currentTask?.task_spec?.requested_dataset ?? currentTask?.recommendation?.primary_dataset ?? null,
                  )}
                </strong>
              </article>
              <article className="result-overview-card">
                <span>时间窗</span>
                <strong>{formatTimeRange(currentTask?.actual_time_range ?? currentTask?.requested_time_range ?? null)}</strong>
              </article>
              <article className="result-overview-card">
                <span>AOI</span>
                <strong>{currentTask?.aoi_name ?? currentTask?.task_spec?.aoi_input ?? "未指定"}</strong>
              </article>
              <article className="result-overview-card">
                <span>图层数</span>
                <strong>{layerRows.filter((row) => row.available).length}</strong>
              </article>
            </div>

            <div className="result-preview">
              {pngArtifact ? <img alt="NDVI 预览" src={pngArtifact.download_url} /> : <p>任务完成后，这里会显示结果预览图。</p>}
            </div>

            <p className="result-overview-note">
              地图视图始终作为主工作区，左侧仅用于成果管理、图层控制和导出操作。
            </p>
          </section>

          <section className="panel-block">
            <div className="block-header">
              <div>
                <p className="block-kicker">Layers</p>
                <h2>图层树与渲染</h2>
              </div>
            </div>

            <div className="layer-tree layer-tree-root">
              <article className="layer-group">
                <div className="layer-group-head">
                  <strong>Analytic Stack</strong>
                  <span>EPSG:3857</span>
                </div>
                <div className="layer-tree-children">
                  {layerRows.map((row) => {
                    const control = layerControls[row.key];
                    return (
                      <article key={row.key} className={`layer-row ${row.available ? "" : "layer-row-disabled"}`}>
                        <div className="layer-row-main">
                          <label className="layer-toggle">
                            <input
                              type="checkbox"
                              checked={control.visible}
                              disabled={!row.available}
                              onChange={(event) => updateLayerControl(row.key, { visible: event.target.checked })}
                            />
                            <div>
                              <strong>{row.title}</strong>
                              <span>{row.subtitle}</span>
                            </div>
                          </label>
                          <span className="panel-pill">{Math.round(control.opacity * 100)}%</span>
                        </div>
                        <input
                          className="opacity-slider"
                          type="range"
                          min="0"
                          max="100"
                          value={Math.round(control.opacity * 100)}
                          disabled={!row.available}
                          onChange={(event) => updateLayerControl(row.key, { opacity: Number(event.target.value) / 100 })}
                        />
                      </article>
                    );
                  })}
                </div>
              </article>
            </div>
          </section>

          <section className="panel-block">
            <div className="block-header">
              <div>
                <p className="block-kicker">Exports</p>
                <h2>下载中心与说明</h2>
              </div>
            </div>

            <div className="artifact-stack artifact-stack-fixed">
              {exportCards.map((card) =>
                card.artifact ? (
                  <a
                    key={card.id}
                    className="artifact-button artifact-button-ready"
                    href={card.artifact.download_url}
                    rel="noreferrer"
                    target="_blank"
                  >
                    <div>
                      <strong>{card.label}</strong>
                      <span>
                        {card.artifact.mime_type} · {formatBytes(card.artifact.size_bytes)}
                      </span>
                    </div>
                    <span>下载文件</span>
                  </a>
                ) : (
                  <div key={card.id} className="artifact-button artifact-button-disabled">
                    <div>
                      <strong>{card.label}</strong>
                      <span>{card.subtitle}</span>
                    </div>
                    <span>待生成</span>
                  </div>
                ),
              )}
            </div>

            <div className="text-preview-stack">
              <article className="text-preview-card">
                <h3>结果摘要</h3>
                <p>{currentTask?.summary_text ?? "当前还没有结果摘要。"}</p>
              </article>
              <article className="text-preview-card">
                <h3>方法说明</h3>
                <p>{currentTask?.methods_text ?? "当前还没有方法说明。"}</p>
              </article>
            </div>
          </section>
        </aside>

        <section className="map-column">
          <GISMap
            geotiffUrl={geotiffArtifact?.download_url ?? null}
            layerControls={layerControls}
            task={currentTask}
            theme={theme}
          />
        </section>

        <aside className="sidebar sidebar-right">
          <section className="panel-block task-console-block">
            <div className="block-header">
              <div>
                <p className="block-kicker">Agent Flow</p>
                <h2>任务流控制台</h2>
              </div>
              <span className="panel-pill">{taskEventsCount} events</span>
            </div>

            <form className="task-form" onSubmit={handleSubmit}>
              <label className="form-label" htmlFor="task-input">
                任务定义
              </label>
              <textarea
                id="task-input"
                value={composerText}
                onChange={(event) => setComposerText(event.target.value)}
                placeholder="输入 AOI、时间窗、数据源偏好与输出要求"
              />

              <div className="form-actions">
                <label className="upload-button">
                  <input type="file" accept=".geojson,.json,.zip" onChange={handleFileChange} />
                  {isUploading ? "上传中..." : "上传 AOI"}
                </label>
                <button type="submit" disabled={isSubmitting || !sessionId}>
                  {isSubmitting ? "提交中..." : "执行任务"}
                </button>
              </div>
            </form>

            {uploads.length ? (
              <div className="upload-list">
                {uploads.map((upload) => (
                  <div key={upload.file_id} className="upload-chip">
                    <strong>{upload.original_name}</strong>
                    <span>{upload.file_type}</span>
                  </div>
                ))}
              </div>
            ) : null}

            <div className="prompt-row prompt-row-compact">
              {QUICK_PROMPTS.map((prompt) => (
                <button key={prompt} className="ghost-button ghost-button-compact" type="button" onClick={() => applyPrompt(prompt)}>
                  {prompt}
                </button>
              ))}
            </div>

            {bootError ? <p className="error-text">{bootError}</p> : null}
            {notice ? <p className="info-text">{notice}</p> : null}
            {uploadError ? <p className="error-text">{uploadError}</p> : null}
            {submitError ? <p className="error-text">{submitError}</p> : null}
            {currentTask?.clarification_message ? <p className="warning-text">{currentTask.clarification_message}</p> : null}
            {currentTask?.error_message ? (
              <p className="error-text">
                {currentTask.error_code ? `${currentTask.error_code}: ` : ""}
                {currentTask.error_message}
              </p>
            ) : null}
          </section>

          <section className="panel-block">
            <div className="block-header">
              <div>
                <p className="block-kicker">Parameters</p>
                <h2>任务参数卡</h2>
              </div>
              <span className="panel-pill">{currentTask?.analysis_type ?? "NDVI"}</span>
            </div>

            {overviewItems.length ? (
              <div className="parameter-grid">
                {overviewItems.map(([label, value]) => (
                  <article key={label} className="parameter-card">
                    <span>{label}</span>
                    <strong>{value}</strong>
                  </article>
                ))}
              </div>
            ) : (
              <p className="empty-text">提交任务后，这里会展示 AOI、时间窗、数据源偏好与输出约束。</p>
            )}
          </section>

          <section className="panel-block">
            <div className="block-header">
              <div>
                <p className="block-kicker">Plan Approval</p>
                <h2>执行计划审批</h2>
              </div>
              <span className={`status-badge status-${formatStatusTone(currentTask?.status)}`}>
                {currentTask?.operation_plan ? `v${currentTask.operation_plan.version}` : "无计划"}
              </span>
            </div>

            {currentTask?.operation_plan ? (
              <>
                <p className="info-text">
                  当前状态：{currentTask.status} · 计划状态：{currentTask.operation_plan.status}
                </p>
                <textarea
                  className="plan-editor"
                  value={planDraftText}
                  onChange={(event) => setPlanDraftText(event.target.value)}
                  spellCheck={false}
                />
                <div className="form-actions">
                  <button
                    type="button"
                    disabled={!canEditOperationPlan || isSavingPlan || isApprovingPlan}
                    onClick={() => {
                      void handleSavePlanDraft();
                    }}
                  >
                    {isSavingPlan ? "保存中..." : "保存计划草稿"}
                  </button>
                  <button
                    type="button"
                    disabled={!canApproveOperationPlan}
                    onClick={() => {
                      void handleApprovePlan();
                    }}
                  >
                    {isApprovingPlan ? "审批中..." : "确认并执行"}
                  </button>
                </div>
              </>
            ) : (
              <p className="empty-text">当前任务还没有可审批的 operation plan。</p>
            )}

            {planError ? <p className="error-text">{planError}</p> : null}
          </section>

          <section className="panel-block">
            <div className="block-header">
              <div>
                <p className="block-kicker">Timeline</p>
                <h2>执行步骤时间线</h2>
              </div>
              <span className={`status-badge status-${formatStatusTone(currentTask?.status)}`}>
                {currentTask?.current_step ?? "等待输入"}
              </span>
            </div>

            {currentTask?.steps.length ? (
              <div className="timeline-list">
                {currentTask.steps.map((step: TaskStep) => (
                  <article key={step.step_name} className="timeline-item">
                    <div className={`timeline-dot timeline-dot-${formatStatusTone(step.status)}`} />
                    <div className="timeline-body">
                      <div className="timeline-head">
                        <strong>{step.step_name}</strong>
                        <span>{formatStatusLabel(step.status)}</span>
                      </div>
                      <p>
                        {formatDateTime(step.started_at)} {"->"} {formatDateTime(step.ended_at)}
                      </p>
                      <div className="detail-pill-row">
                        {getDetailEntries(step.observation ?? step.detail).map(([label, value]) => (
                          <span key={`${step.step_name}-${label}`} className="detail-pill">
                            {label}: {value}
                          </span>
                        ))}
                      </div>
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <p className="empty-text">任务开始执行后，这里会展示解析、搜索、推荐、合成和导出步骤。</p>
            )}
          </section>

          <section className="panel-block">
            <div className="block-header">
              <div>
                <p className="block-kicker">Candidates</p>
                <h2>数据候选比较</h2>
              </div>
            </div>

            {currentTask?.candidates.length ? (
              <div className="candidate-stack">
                {currentTask.candidates.map((candidate: Candidate) => (
                  <article key={candidate.dataset_name} className="candidate-card">
                    <div className="candidate-head">
                      <div>
                        <h3>{formatDatasetName(candidate.dataset_name)}</h3>
                        <p>
                          {candidate.collection_id} · {formatSourceLabel(candidate.summary_json?.source)}
                        </p>
                      </div>
                      <span className="panel-pill">#{candidate.recommendation_rank}</span>
                    </div>
                    <div className="candidate-metrics">
                      <div>
                        <span>景数</span>
                        <strong>{candidate.scene_count}</strong>
                      </div>
                      <div>
                        <span>覆盖率</span>
                        <strong>{formatPercent(candidate.coverage_ratio)}</strong>
                      </div>
                      <div>
                        <span>有效像元</span>
                        <strong>{formatPercent(candidate.effective_pixel_ratio_estimate)}</strong>
                      </div>
                      <div>
                        <span>目录云量</span>
                        <strong>{formatCloudPercent(candidate.cloud_metric_summary?.median)}</strong>
                      </div>
                      <div>
                        <span>分辨率</span>
                        <strong>{candidate.spatial_resolution} m</strong>
                      </div>
                      <div>
                        <span>适配分</span>
                        <strong>{candidate.suitability_score.toFixed(2)}</strong>
                      </div>
                    </div>
                    <div className="detail-pill-row">
                      <span className="detail-pill">频率 {candidate.temporal_density_note}</span>
                      {currentTask.recommendation?.primary_dataset === candidate.dataset_name ? (
                        <span className="detail-pill">当前首选</span>
                      ) : null}
                      {currentTask.recommendation?.primary_dataset === candidate.dataset_name &&
                      currentTask.recommendation?.confidence !== null &&
                      currentTask.recommendation?.confidence !== undefined ? (
                        <span className="detail-pill">可信度 {formatPercent(currentTask.recommendation.confidence)}</span>
                      ) : null}
                      {currentTask.task_spec?.requested_dataset === candidate.dataset_name ? (
                        <span className="detail-pill">用户指定</span>
                      ) : null}
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <p className="empty-text">候选搜索完成后，这里会展示不同数据源的对比结果。</p>
            )}
          </section>

          <section className="panel-block">
            <div className="block-header">
              <div>
                <p className="block-kicker">Follow-up</p>
                <h2>追问操作</h2>
              </div>
            </div>

            {rerunActions.length ? (
              <div className="action-grid">
                {rerunActions.map((action) => (
                  <button
                    key={action.id}
                    className="action-card"
                    type="button"
                    disabled={activeActionId !== null}
                    onClick={() => handleRerun(action.id, action.override)}
                  >
                    <strong>{activeActionId === action.id ? "处理中..." : action.label}</strong>
                    <span>{action.description}</span>
                  </button>
                ))}
              </div>
            ) : (
              <p className="empty-text">完成一条任务后，这里会出现快速重跑和追问操作。</p>
            )}

            <div className="prompt-row prompt-row-compact">
              {FOLLOWUP_PROMPTS.map((prompt) => (
                <button key={prompt} className="ghost-button ghost-button-compact" type="button" onClick={() => applyPrompt(prompt)}>
                  {prompt}
                </button>
              ))}
            </div>
          </section>

          <section className="panel-block">
            <div className="block-header">
              <div>
                <p className="block-kicker">History</p>
                <h2>最近任务</h2>
              </div>
            </div>

            {currentHistory.length ? (
              <div className="history-list">
                {currentHistory.map((historyItem) => (
                  <button
                    key={historyItem.task_id}
                    className={`history-card ${historyItem.task_id === currentTask?.task_id ? "history-card-active" : ""}`}
                    type="button"
                    onClick={() => handleHistorySelect(historyItem.task_id)}
                  >
                    <div className="history-card-head">
                      <strong>{historyItem.task_id}</strong>
                      <span className={`status-badge status-${formatStatusTone(historyItem.status)}`}>
                        {formatStatusLabel(historyItem.status)}
                      </span>
                    </div>
                    <p>{historyItem.task_spec?.aoi_input ?? "未记录 AOI"}</p>
                    <span>{formatDateTime(historyItem.created_at)}</span>
                  </button>
                ))}
              </div>
            ) : (
              <p className="empty-text">当前 session 还没有历史任务。</p>
            )}
          </section>
        </aside>
      </main>
    </div>
  );
}
