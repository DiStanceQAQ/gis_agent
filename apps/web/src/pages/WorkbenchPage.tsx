import { useDeferredValue } from "react";
import LeftColumn from "../components/workbench/LeftColumn";
import RightColumn from "../components/workbench/RightColumn";
import TopNavBar from "../components/workbench/TopNavBar";
import type { StatusTone } from "../components/workbench/types";
import GISMap from "../components/GISMap";
import { isMapRenderableArtifact, toggleTheme, type WorkbenchState } from "../hooks/useWorkbenchState";
import type { Artifact } from "../types";

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
    case "cancelled":
      return "已取消";
    default:
      return "待开始";
  }
}

function formatSystemStatus(status?: string | null): string {
  switch (status) {
    case "running":
      return "运行中";
    case "queued":
      return "排队中";
    case "success":
      return "已完成";
    case "failed":
      return "异常";
    case "awaiting_approval":
      return "等待审批";
    case "waiting_clarification":
      return "待补充";
    case "cancelled":
      return "已取消";
    default:
      return "空闲";
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

function formatDateTime(value?: string | null): string {
  if (!value) {
    return "--";
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

function sourceKindLabel(kind: ReturnType<WorkbenchState["getUploadSourceKind"]>): string {
  switch (kind) {
    case "raster":
      return "本地栅格";
    case "vector":
      return "本地矢量";
    default:
      return "本地数据";
  }
}

function artifactTypeLabel(artifact: Artifact): string {
  switch (artifact.artifact_type) {
    case "geotiff":
      return "GeoTIFF 结果";
    case "geojson":
    case "vector_geojson":
      return "GeoJSON 结果";
    default:
      return `结果 · ${artifact.artifact_type}`;
  }
}

type TaskNodeLike = {
  task_id: string;
  parent_task_id?: string | null;
};

function buildTaskThreadIds(currentTaskId: string | null, nodes: TaskNodeLike[]): Set<string> {
  if (!currentTaskId) {
    return new Set();
  }

  const byId = new Map<string, TaskNodeLike>();
  const childrenByParent = new Map<string, string[]>();

  for (const node of nodes) {
    byId.set(node.task_id, node);
    if (node.parent_task_id) {
      const siblings = childrenByParent.get(node.parent_task_id) ?? [];
      siblings.push(node.task_id);
      childrenByParent.set(node.parent_task_id, siblings);
    }
  }

  let rootId = currentTaskId;
  while (true) {
    const node = byId.get(rootId);
    if (!node?.parent_task_id || !byId.has(node.parent_task_id)) {
      break;
    }
    rootId = node.parent_task_id;
  }

  const visited = new Set<string>();
  const queue = [rootId];
  while (queue.length) {
    const taskId = queue.shift();
    if (!taskId || visited.has(taskId)) {
      continue;
    }
    visited.add(taskId);
    const children = childrenByParent.get(taskId) ?? [];
    queue.push(...children);
  }

  visited.add(currentTaskId);
  return visited;
}

type WorkbenchPageProps = {
  state: WorkbenchState;
};

export default function WorkbenchPage({ state }: WorkbenchPageProps) {
  const currentTask = useDeferredValue(state.task);
  const currentHistory = useDeferredValue(state.taskHistory);

  const canEditOperationPlan = currentTask?.status === "awaiting_approval" && Boolean(currentTask.operation_plan);
  const canApproveOperationPlan = canEditOperationPlan && !state.isSavingPlan && !state.isApprovingPlan;

  const uploadLayerRows = state.uploads.map((upload) => {
    const preview = state.uploadedLayerPreviews[upload.file_id];
    const status = state.uploadPreviewStatuses[upload.file_id] ?? "pending";
    const mapSupported =
      preview?.kind === "raster"
        ? Boolean(preview.rasterPreviewImageUrl && preview.previewBounds)
        : preview?.kind === "vector"
          ? Boolean(preview.geojsonText)
          : false;

    return {
      fileId: upload.file_id,
      title: upload.original_name,
      subtitle: sourceKindLabel(state.getUploadSourceKind(upload)),
      mapSupported,
      status,
    };
  });

  const artifactLayerRows = (currentTask?.artifacts ?? []).map((artifact) => ({
    artifactId: artifact.artifact_id,
    title: artifactTypeLabel(artifact),
    subtitle: `${artifact.mime_type} · ${artifact.artifact_id.slice(-6)}`,
    mapSupported: isMapRenderableArtifact(artifact),
  }));

  const threadTaskIds = buildTaskThreadIds(
    currentTask?.task_id ?? null,
    currentTask ? [
      ...currentHistory,
      {
        task_id: currentTask.task_id,
        parent_task_id: currentTask.parent_task_id,
      },
    ] : currentHistory,
  );

  const visibleSessionMessages = threadTaskIds.size
    ? state.messages.filter((message) => !message.linked_task_id || threadTaskIds.has(message.linked_task_id))
    : state.messages;

  return (
    <div className="wb-shell">
      <TopNavBar
        theme={state.theme}
        onToggleTheme={() => state.setTheme((current) => toggleTheme(current))}
        sessionId={state.sessionId}
        statusLabel={formatSystemStatus(currentTask?.status)}
      />

      <main className="wb-workspace">
        <LeftColumn
          isUploading={state.isUploading}
          uploadError={state.uploadError}
          onUploadFile={state.handleFileChange}
          uploadLayerControls={state.uploadLayerControls}
          artifactLayerControls={state.artifactLayerControls}
          uploadLayerRows={uploadLayerRows}
          artifactLayerRows={artifactLayerRows}
          onUpdateUploadLayerControl={state.updateUploadLayerControl}
          onUpdateArtifactLayerControl={state.updateArtifactLayerControl}
          onLocateUploadLayer={state.focusUploadLayer}
          onLocateArtifactLayer={state.focusArtifactLayer}
          onRemoveUploadLayer={state.removeUploadedFile}
        />

        <section className="wb-map-column">
          <GISMap
            layerControls={state.layerControls}
            uploadedLayerPreviews={state.uploadedLayerPreviews}
            uploadLayerControls={state.uploadLayerControls}
            focusRequest={state.mapFocusRequest}
            artifacts={currentTask?.artifacts ?? []}
            artifactLayerControls={state.artifactLayerControls}
            task={currentTask}
            theme={state.theme}
          />
        </section>

        <RightColumn
          currentTaskId={currentTask?.task_id ?? null}
          taskHistory={currentHistory}
          currentTask={currentTask}
          latestResponseMode={state.latestUnderstandingSnapshot.responseMode}
          latestUnderstanding={state.latestUnderstandingSnapshot.understanding}
          latestResponsePayload={state.latestUnderstandingSnapshot.responsePayload}
          sessionMessages={visibleSessionMessages}
          streamingAssistantMessage={state.streamingAssistantMessage}
          messagesNextCursor={state.messagesNextCursor}
          isLoadingMessages={state.isLoadingMessages}
          messagesError={state.messagesError}
          streamMode={state.streamMode}
          composerText={state.composerText}
          setComposerText={state.setComposerText}
          submitError={state.submitError}
          notice={state.notice}
          planError={state.planError}
          isSubmitting={state.isSubmitting}
          isSavingPlan={state.isSavingPlan}
          isApprovingPlan={state.isApprovingPlan}
          isRejectingPlan={state.isRejectingPlan}
          planDraftText={state.planDraftText}
          setPlanDraftText={state.setPlanDraftText}
          handleSubmit={state.handleSubmit}
          handleSavePlanDraft={state.handleSavePlanDraft}
          handleApprovePlan={state.handleApprovePlan}
          handleRejectPlan={state.handleRejectPlan}
          handleLoadMoreMessages={state.loadMoreMessages}
          onHistorySelect={state.handleHistorySelect}
          canEditOperationPlan={canEditOperationPlan}
          canApproveOperationPlan={canApproveOperationPlan}
          formatStatusLabel={formatStatusLabel}
          formatStatusTone={formatStatusTone}
          formatDateTime={formatDateTime}
        />
      </main>
    </div>
  );
}
