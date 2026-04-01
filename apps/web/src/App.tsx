import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { createMessage, createSession, getTask, uploadFile } from "./api";
import type { Artifact, TaskDetail, UploadResponse } from "./types";

const TERMINAL_STATES = new Set(["success", "failed", "waiting_clarification"]);

function formatDatasetName(name: string): string {
  if (name === "sentinel2") return "Sentinel-2";
  if (name === "landsat89") return "Landsat 8/9";
  return name;
}

export default function App() {
  const [sessionId, setSessionId] = useState<string>("");
  const [prompt, setPrompt] = useState(
    "帮我计算 2024 年 6 到 8 月北京西山的 NDVI，并导出地图和 GeoTIFF。",
  );
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [uploads, setUploads] = useState<UploadResponse[]>([]);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [error, setError] = useState<string>("");
  const poller = useRef<number | null>(null);

  useEffect(() => {
    createSession()
      .then((data) => setSessionId(data.session_id))
      .catch((err: Error) => setError(err.message));
    return () => {
      if (poller.current) window.clearInterval(poller.current);
    };
  }, []);

  const artifactMap = useMemo<Record<string, Artifact>>(() => {
    if (!task) return {};
    return Object.fromEntries(task.artifacts.map((artifact) => [artifact.artifact_type, artifact]));
  }, [task]);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!sessionId) return;
    setError("");
    setIsSubmitting(true);

    try {
      const message = await createMessage({
        session_id: sessionId,
        content: prompt,
        file_ids: uploads.map((item) => item.file_id),
      });
      const initialTask = await getTask(message.task_id);
      setTask(initialTask);

      if (poller.current) window.clearInterval(poller.current);
      poller.current = window.setInterval(async () => {
        const latest = await getTask(message.task_id);
        setTask(latest);
        if (TERMINAL_STATES.has(latest.status)) {
          if (poller.current) window.clearInterval(poller.current);
        }
      }, 1500);
    } catch (err) {
      setError(err instanceof Error ? err.message : "提交失败");
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleFileChange(fileList: FileList | null) {
    if (!sessionId || !fileList?.length) return;
    setIsUploading(true);
    setError("");

    try {
      const nextUploads: UploadResponse[] = [];
      for (const file of Array.from(fileList)) {
        const uploaded = await uploadFile(sessionId, file);
        nextUploads.push(uploaded);
      }
      setUploads((current) => current.concat(nextUploads));
    } catch (err) {
      setError(err instanceof Error ? err.message : "上传失败");
    } finally {
      setIsUploading(false);
    }
  }

  return (
    <div className="page-shell">
      <aside className="hero-card">
        <p className="eyebrow">GIS Agent MVP Scaffold</p>
        <h1>用一条可运行骨架，把 PRD 变成能继续堆功能的项目。</h1>
        <p className="hero-copy">
          当前默认跑 mock pipeline，用来验证任务创建、状态推进、结果下载和结果页结构。
        </p>
        <dl className="summary-grid">
          <div>
            <dt>Session</dt>
            <dd>{sessionId || "初始化中..."}</dd>
          </div>
          <div>
            <dt>Execution</dt>
            <dd>inline_mock / celery_mock</dd>
          </div>
          <div>
            <dt>P0 Data</dt>
            <dd>Sentinel-2, Landsat 8/9</dd>
          </div>
          <div>
            <dt>Pipeline</dt>
            <dd>NDVI + median + exports</dd>
          </div>
        </dl>
      </aside>

      <main className="workspace">
        <section className="panel">
          <div className="panel-header">
            <h2>对话输入</h2>
            <span className="badge">{task?.status ?? "draft"}</span>
          </div>
          <form className="composer" onSubmit={handleSubmit}>
            <textarea
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              rows={5}
              placeholder="输入一个 NDVI 任务"
            />
            <div className="composer-row">
              <label className="file-picker">
                <span>{isUploading ? "上传中..." : "上传 AOI 文件"}</span>
                <input
                  type="file"
                  multiple
                  onChange={(event) => handleFileChange(event.target.files)}
                />
              </label>
              <button type="submit" disabled={!sessionId || isSubmitting}>
                {isSubmitting ? "提交中..." : "创建任务"}
              </button>
            </div>
          </form>

          {!!uploads.length && (
            <div className="upload-list">
              {uploads.map((upload) => (
                <div className="upload-chip" key={upload.file_id}>
                  <strong>{upload.original_name}</strong>
                  <span>{upload.file_type}</span>
                </div>
              ))}
            </div>
          )}

          {error && <p className="error-text">{error}</p>}
        </section>

        <section className="panel">
          <div className="panel-header">
            <h2>任务状态</h2>
            <span className="meta">{task?.task_id ?? "未创建"}</span>
          </div>
          <div className="status-list">
            {(task?.steps ?? []).map((step) => (
              <div className="status-item" key={step.step_name}>
                <div>
                  <strong>{step.step_name}</strong>
                  <p>{step.status}</p>
                </div>
                <span className={`dot dot-${step.status}`}></span>
              </div>
            ))}
            {!task?.steps.length && <p className="muted">任务创建后会在这里展示步骤状态。</p>}
          </div>
        </section>

        <section className="panel panel-wide">
          <div className="panel-header">
            <h2>结果页</h2>
            <span className="meta">{task?.analysis_type ?? "NDVI"}</span>
          </div>

          {task?.task_spec && (
            <div className="task-summary">
              <p>
                <strong>研究区：</strong>
                {String(task.task_spec.aoi_input ?? "-")}
              </p>
              <p>
                <strong>时间范围：</strong>
                {task.requested_time_range
                  ? `${task.requested_time_range.start} ~ ${task.requested_time_range.end}`
                  : "-"}
              </p>
            </div>
          )}

          {task?.recommendation && (
            <div className="recommendation-card">
              <h3>数据推荐</h3>
              <p>
                <strong>首选：</strong>
                {formatDatasetName(task.recommendation.primary_dataset)}
              </p>
              <p>
                <strong>备选：</strong>
                {task.recommendation.backup_dataset
                  ? formatDatasetName(task.recommendation.backup_dataset)
                  : "无"}
              </p>
              <p>{task.recommendation.reason}</p>
              {task.recommendation.risk_note && (
                <p className="muted">{task.recommendation.risk_note}</p>
              )}
            </div>
          )}

          {!!task?.candidates.length && (
            <div className="candidate-grid">
              {task.candidates.map((candidate) => (
                <article className="candidate-card" key={candidate.dataset_name}>
                  <h3>{formatDatasetName(candidate.dataset_name)}</h3>
                  <p>推荐排序：{candidate.recommendation_rank}</p>
                  <p>场景数：{candidate.scene_count}</p>
                  <p>覆盖率：{Math.round(candidate.coverage_ratio * 100)}%</p>
                  <p>分辨率：{candidate.spatial_resolution}m</p>
                  <p>评分：{candidate.suitability_score.toFixed(2)}</p>
                </article>
              ))}
            </div>
          )}

          <div className="artifact-layout">
            <div className="preview-box">
              {task?.png_preview_url ? (
                <img src={task.png_preview_url} alt="GIS Agent map preview" />
              ) : (
                <div className="preview-placeholder">地图预览会显示在这里</div>
              )}
            </div>
            <div className="artifact-list">
              <h3>下载</h3>
              {(task?.artifacts ?? []).map((artifact) => (
                <a
                  className="artifact-link"
                  href={artifact.download_url}
                  key={artifact.artifact_id}
                  target="_blank"
                  rel="noreferrer"
                >
                  <span>{artifact.artifact_type}</span>
                  <span>{Math.round(artifact.size_bytes / 1024)} KB</span>
                </a>
              ))}
            </div>
          </div>

          <div className="text-columns">
            <article>
              <h3>结果摘要</h3>
              <p>{task?.summary_text ?? "任务运行后会生成摘要。"}</p>
            </article>
            <article>
              <h3>方法说明</h3>
              <p>{task?.methods_text ?? "任务运行后会生成方法说明。"}</p>
            </article>
          </div>

          {task?.clarification_message && task.status === "waiting_clarification" && (
            <p className="muted">{task.clarification_message}</p>
          )}
          {task?.error_message && <p className="error-text">{task.error_message}</p>}
          {!artifactMap.geotiff && task?.status === "success" && (
            <p className="muted">当前结果来自 mock pipeline，后续可替换为真实 STAC + NDVI 工作流。</p>
          )}
        </section>
      </main>
    </div>
  );
}
