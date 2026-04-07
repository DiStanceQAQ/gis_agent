import type { Candidate, TaskStep } from "../../types";
import type { RightColumnProps } from "./types";

export default function RightColumn({
  currentTask,
  currentHistory,
  taskEventsCount,
  composerText,
  setComposerText,
  quickPrompts,
  followupPrompts,
  uploads,
  bootError,
  notice,
  uploadError,
  submitError,
  planError,
  isUploading,
  isSubmitting,
  isSavingPlan,
  isApprovingPlan,
  activeActionId,
  planDraftText,
  setPlanDraftText,
  rerunActions,
  overviewItems,
  handleSubmit,
  handleFileChange,
  applyPrompt,
  handleSavePlanDraft,
  handleApprovePlan,
  handleRerun,
  handleHistorySelect,
  canEditOperationPlan,
  canApproveOperationPlan,
  formatStatusLabel,
  formatStatusTone,
  formatDateTime,
  formatDatasetName,
  formatPercent,
  formatCloudPercent,
  formatSourceLabel,
  getDetailEntries,
}: RightColumnProps) {
  return (
    <aside className="wb-right-column">
      <section className="wb-panel">
        <div className="wb-panel-head">
          <div>
            <p className="wb-kicker">Agent Flow</p>
            <h2>任务控制台</h2>
          </div>
          <span className="wb-pill">{taskEventsCount} events</span>
        </div>

        <form className="wb-form" onSubmit={handleSubmit}>
          <label htmlFor="task-input">任务定义</label>
          <textarea
            id="task-input"
            value={composerText}
            onChange={(event) => setComposerText(event.target.value)}
            placeholder="输入 AOI、时间窗、数据源偏好与输出要求"
          />

          <div className="wb-form-actions">
            <label className="wb-upload-btn">
              <input type="file" accept=".geojson,.json,.zip" onChange={handleFileChange} />
              {isUploading ? "上传中..." : "上传 AOI"}
            </label>
            <button type="submit" disabled={isSubmitting}>
              {isSubmitting ? "提交中..." : "执行任务"}
            </button>
          </div>
        </form>

        {uploads.length ? (
          <div className="wb-upload-list">
            {uploads.map((upload) => (
              <div key={upload.file_id} className="wb-upload-chip">
                <strong>{upload.original_name}</strong>
                <span>{upload.file_type}</span>
              </div>
            ))}
          </div>
        ) : null}

        <div className="wb-prompt-row">
          {quickPrompts.map((prompt) => (
            <button key={prompt} className="wb-ghost-btn wb-ghost-btn-compact" type="button" onClick={() => applyPrompt(prompt)}>
              {prompt}
            </button>
          ))}
        </div>

        {bootError ? <p className="wb-text wb-text-error">{bootError}</p> : null}
        {notice ? <p className="wb-text wb-text-info">{notice}</p> : null}
        {uploadError ? <p className="wb-text wb-text-error">{uploadError}</p> : null}
        {submitError ? <p className="wb-text wb-text-error">{submitError}</p> : null}
        {currentTask?.clarification_message ? <p className="wb-text wb-text-warn">{currentTask.clarification_message}</p> : null}
        {currentTask?.error_message ? (
          <p className="wb-text wb-text-error">
            {currentTask.error_code ? `${currentTask.error_code}: ` : ""}
            {currentTask.error_message}
          </p>
        ) : null}
      </section>

      <section className="wb-panel">
        <div className="wb-panel-head">
          <div>
            <p className="wb-kicker">Parameters</p>
            <h2>任务参数</h2>
          </div>
          <span className="wb-pill">{currentTask?.analysis_type ?? "NDVI"}</span>
        </div>

        {overviewItems.length ? (
          <div className="wb-param-grid">
            {overviewItems.map(([label, value]) => (
              <article key={label}>
                <span>{label}</span>
                <strong>{value}</strong>
              </article>
            ))}
          </div>
        ) : (
          <p className="wb-text">提交任务后，这里会展示 AOI、时间窗、数据源偏好与输出约束。</p>
        )}
      </section>

      <section className="wb-panel">
        <div className="wb-panel-head">
          <div>
            <p className="wb-kicker">Plan Approval</p>
            <h2>执行计划审批</h2>
          </div>
          <span className={`wb-badge wb-badge-${formatStatusTone(currentTask?.status)}`}>
            {currentTask?.operation_plan ? `v${currentTask.operation_plan.version}` : "无计划"}
          </span>
        </div>

        {currentTask?.operation_plan ? (
          <>
            <p className="wb-text wb-text-info">
              当前状态：{currentTask.status} · 计划状态：{currentTask.operation_plan.status}
            </p>
            <textarea
              className="wb-plan-editor"
              value={planDraftText}
              onChange={(event) => setPlanDraftText(event.target.value)}
              spellCheck={false}
            />

            <div className="wb-form-actions">
              <button
                type="button"
                disabled={!canEditOperationPlan || isSavingPlan || isApprovingPlan}
                onClick={() => {
                  void handleSavePlanDraft();
                }}
              >
                {isSavingPlan ? "保存中..." : "保存草稿"}
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
          <p className="wb-text">当前任务还没有可审批的 operation plan。</p>
        )}

        {planError ? <p className="wb-text wb-text-error">{planError}</p> : null}
      </section>

      <section className="wb-panel">
        <div className="wb-panel-head">
          <div>
            <p className="wb-kicker">Timeline</p>
            <h2>执行步骤</h2>
          </div>
          <span className={`wb-badge wb-badge-${formatStatusTone(currentTask?.status)}`}>
            {currentTask?.current_step ?? "等待输入"}
          </span>
        </div>

        {currentTask?.steps.length ? (
          <div className="wb-timeline">
            {currentTask.steps.map((step: TaskStep) => (
              <article key={step.step_name} className="wb-timeline-item">
                <div className={`wb-timeline-dot wb-dot-${formatStatusTone(step.status)}`} />
                <div className="wb-timeline-body">
                  <div className="wb-timeline-head">
                    <strong>{step.step_name}</strong>
                    <span>{formatStatusLabel(step.status)}</span>
                  </div>
                  <p>
                    {formatDateTime(step.started_at)} {"->"} {formatDateTime(step.ended_at)}
                  </p>
                  <div className="wb-detail-row">
                    {getDetailEntries(step.observation ?? step.detail).map(([label, value]) => (
                      <span key={`${step.step_name}-${label}`} className="wb-pill">
                        {label}: {value}
                      </span>
                    ))}
                  </div>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <p className="wb-text">任务开始执行后，这里会展示解析、推荐、处理与导出步骤。</p>
        )}
      </section>

      <section className="wb-panel">
        <div className="wb-panel-head">
          <div>
            <p className="wb-kicker">Candidates</p>
            <h2>数据候选</h2>
          </div>
        </div>

        {currentTask?.candidates.length ? (
          <div className="wb-candidate-stack">
            {currentTask.candidates.map((candidate: Candidate) => (
              <article key={candidate.dataset_name} className="wb-candidate-card">
                <div className="wb-candidate-head">
                  <div>
                    <h3>{formatDatasetName(candidate.dataset_name)}</h3>
                    <p>
                      {candidate.collection_id} · {formatSourceLabel(candidate.summary_json?.source)}
                    </p>
                  </div>
                  <span className="wb-pill">#{candidate.recommendation_rank}</span>
                </div>

                <div className="wb-candidate-grid">
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
                    <span>云量</span>
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

                <div className="wb-detail-row">
                  <span className="wb-pill">频率 {candidate.temporal_density_note}</span>
                  {currentTask.recommendation?.primary_dataset === candidate.dataset_name ? (
                    <span className="wb-pill">当前首选</span>
                  ) : null}
                  {currentTask.task_spec?.requested_dataset === candidate.dataset_name ? (
                    <span className="wb-pill">用户指定</span>
                  ) : null}
                </div>
              </article>
            ))}
          </div>
        ) : (
          <p className="wb-text">候选搜索完成后，这里会展示不同数据源的对比结果。</p>
        )}
      </section>

      <section className="wb-panel">
        <div className="wb-panel-head">
          <div>
            <p className="wb-kicker">Follow-up</p>
            <h2>追问与重跑</h2>
          </div>
        </div>

        {rerunActions.length ? (
          <div className="wb-action-grid">
            {rerunActions.map((action) => (
              <button
                key={action.id}
                className="wb-action-card"
                type="button"
                disabled={activeActionId !== null}
                onClick={() => {
                  void handleRerun(action.id, action.override);
                }}
              >
                <strong>{activeActionId === action.id ? "处理中..." : action.label}</strong>
                <span>{action.description}</span>
              </button>
            ))}
          </div>
        ) : (
          <p className="wb-text">完成一条任务后，这里会出现快速重跑和追问操作。</p>
        )}

        <div className="wb-prompt-row">
          {followupPrompts.map((prompt) => (
            <button key={prompt} className="wb-ghost-btn wb-ghost-btn-compact" type="button" onClick={() => applyPrompt(prompt)}>
              {prompt}
            </button>
          ))}
        </div>
      </section>

      <section className="wb-panel">
        <div className="wb-panel-head">
          <div>
            <p className="wb-kicker">History</p>
            <h2>最近任务</h2>
          </div>
        </div>

        {currentHistory.length ? (
          <div className="wb-history-stack">
            {currentHistory.map((historyItem) => (
              <button
                key={historyItem.task_id}
                className={`wb-history-card ${historyItem.task_id === currentTask?.task_id ? "wb-history-card-active" : ""}`}
                type="button"
                onClick={() => handleHistorySelect(historyItem.task_id)}
              >
                <div className="wb-history-head">
                  <strong>{historyItem.task_id}</strong>
                  <span className={`wb-badge wb-badge-${formatStatusTone(historyItem.status)}`}>
                    {formatStatusLabel(historyItem.status)}
                  </span>
                </div>
                <p>{historyItem.task_spec?.aoi_input ?? "未记录 AOI"}</p>
                <span>{formatDateTime(historyItem.created_at)}</span>
              </button>
            ))}
          </div>
        ) : (
          <p className="wb-text">当前 session 还没有历史任务。</p>
        )}
      </section>
    </aside>
  );
}
