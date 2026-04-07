import { useDeferredValue } from "react";
import LeftColumn from "../components/workbench/LeftColumn";
import RightColumn from "../components/workbench/RightColumn";
import TopNavBar from "../components/workbench/TopNavBar";
import type { StatusTone } from "../components/workbench/types";
import GISMap from "../components/GISMap";
import { toggleTheme, type WorkbenchState } from "../hooks/useWorkbenchState";
import type { Artifact, TaskDetail, TaskSpec, TimeRange } from "../types";

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
    case "awaiting_approval":
      return "等待审批";
    default:
      return "待开始";
  }
}

function formatStatusTone(status?: string | null): StatusTone {
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
    case "awaiting_approval":
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
    ["优先输出", taskSpec?.preferred_output?.join(" / ") ?? "默认"],
    ["创建方式", taskSpec?.created_from ?? "用户首条请求"],
  ];
}

function getPrimaryArtifact(task?: TaskDetail | null, artifactType?: string): Artifact | undefined {
  return task?.artifacts.find((artifact) => artifact.artifact_type === artifactType);
}

type WorkbenchPageProps = {
  state: WorkbenchState;
};

export default function WorkbenchPage({ state }: WorkbenchPageProps) {
  const currentTask = useDeferredValue(state.task);
  const currentHistory = useDeferredValue(state.taskHistory);

  const pngArtifact = getPrimaryArtifact(currentTask, "png_map");
  const geotiffArtifact = getPrimaryArtifact(currentTask, "geotiff");
  const methodsArtifact = getPrimaryArtifact(currentTask, "methods_text");
  const summaryArtifact = getPrimaryArtifact(currentTask, "summary_text");

  const hasResult = currentTask?.status === "success";
  const overviewItems = getOverviewItems(currentTask?.task_spec, currentTask);
  const canEditOperationPlan = currentTask?.status === "awaiting_approval" && Boolean(currentTask.operation_plan);
  const canApproveOperationPlan = canEditOperationPlan && !state.isSavingPlan && !state.isApprovingPlan;

  const layerRows = [
    {
      key: "basemap" as const,
      title: "基础底图",
      subtitle: "OSM 底图",
      available: true,
    },
    {
      key: "aoi" as const,
      title: "研究区边界",
      subtitle: currentTask?.aoi_name ?? currentTask?.task_spec?.aoi_input ?? "等待 AOI",
      available: Boolean(currentTask?.aoi_bbox_bounds?.length),
    },
    {
      key: "ndvi" as const,
      title: "指数栅格",
      subtitle: geotiffArtifact ? `${formatBytes(geotiffArtifact.size_bytes)} · GeoTIFF` : "等待 GeoTIFF 产物",
      available: Boolean(geotiffArtifact),
    },
  ];

  const exportCards = [
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

  return (
    <div className="wb-shell">
      <TopNavBar
        theme={state.theme}
        onToggleTheme={() => state.setTheme((current) => toggleTheme(current))}
        sessionId={state.sessionId}
        taskId={currentTask?.task_id ?? null}
        statusTone={formatStatusTone(currentTask?.status)}
        statusLabel={formatStatusLabel(currentTask?.status)}
        datasetLabel={formatDatasetName(
          currentTask?.task_spec?.requested_dataset ?? currentTask?.recommendation?.primary_dataset ?? null,
        )}
        statusText={`${currentTask?.current_step ?? "等待任务输入"} · ${
          currentTask?.aoi_name ?? currentTask?.task_spec?.aoi_input ?? "未设置 AOI"
        }`}
      />

      <main className="wb-workspace">
        <LeftColumn
          hasResult={hasResult}
          datasetLabel={formatDatasetName(
            currentTask?.task_spec?.requested_dataset ?? currentTask?.recommendation?.primary_dataset ?? null,
          )}
          timeRangeLabel={formatTimeRange(currentTask?.actual_time_range ?? currentTask?.requested_time_range ?? null)}
          aoiLabel={currentTask?.aoi_name ?? currentTask?.task_spec?.aoi_input ?? "未指定"}
          layerRows={layerRows}
          layerControls={state.layerControls}
          onUpdateLayerControl={state.updateLayerControl}
          exportCards={exportCards}
          summaryText={currentTask?.summary_text ?? "当前还没有结果摘要。"}
          methodsText={currentTask?.methods_text ?? "当前还没有方法说明。"}
          previewUrl={pngArtifact?.download_url ?? null}
          formatBytes={formatBytes}
        />

        <section className="wb-map-column">
          <GISMap
            geotiffUrl={geotiffArtifact?.download_url ?? null}
            layerControls={state.layerControls}
            task={currentTask}
            theme={state.theme}
          />
        </section>

        <RightColumn
          currentTask={currentTask}
          currentHistory={currentHistory}
          taskEventsCount={state.taskEventsCount}
          composerText={state.composerText}
          setComposerText={state.setComposerText}
          quickPrompts={state.quickPrompts}
          followupPrompts={state.followupPrompts}
          uploads={state.uploads}
          bootError={state.bootError}
          notice={state.notice}
          uploadError={state.uploadError}
          submitError={state.submitError}
          planError={state.planError}
          isUploading={state.isUploading}
          isSubmitting={state.isSubmitting}
          isSavingPlan={state.isSavingPlan}
          isApprovingPlan={state.isApprovingPlan}
          activeActionId={state.activeActionId}
          planDraftText={state.planDraftText}
          setPlanDraftText={state.setPlanDraftText}
          rerunActions={state.rerunActions}
          overviewItems={overviewItems}
          handleSubmit={state.handleSubmit}
          handleFileChange={state.handleFileChange}
          applyPrompt={state.applyPrompt}
          handleSavePlanDraft={state.handleSavePlanDraft}
          handleApprovePlan={state.handleApprovePlan}
          handleRerun={state.handleRerun}
          handleHistorySelect={state.handleHistorySelect}
          canEditOperationPlan={canEditOperationPlan}
          canApproveOperationPlan={canApproveOperationPlan}
          formatStatusLabel={formatStatusLabel}
          formatStatusTone={formatStatusTone}
          formatDateTime={formatDateTime}
          formatDatasetName={formatDatasetName}
          formatPercent={formatPercent}
          formatCloudPercent={formatCloudPercent}
          formatSourceLabel={formatSourceLabel}
          getDetailEntries={getDetailEntries}
        />
      </main>
    </div>
  );
}
