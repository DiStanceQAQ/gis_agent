import type { LeftColumnProps } from "./types";

export default function LeftColumn({
  hasResult,
  datasetLabel,
  timeRangeLabel,
  aoiLabel,
  layerRows,
  layerControls,
  onUpdateLayerControl,
  exportCards,
  summaryText,
  methodsText,
  previewUrl,
  formatBytes,
}: LeftColumnProps) {
  return (
    <aside className="wb-left-column">
      <section className="wb-panel">
        <div className="wb-panel-head">
          <div>
            <p className="wb-kicker">Results</p>
            <h2>成果总览</h2>
          </div>
          <span className={`wb-pill ${hasResult ? "wb-pill-highlight" : ""}`}>{hasResult ? "结果可用" : "等待执行"}</span>
        </div>

        <div className="wb-metrics-grid">
          <article>
            <span>数据源</span>
            <strong>{datasetLabel}</strong>
          </article>
          <article>
            <span>时间窗</span>
            <strong>{timeRangeLabel}</strong>
          </article>
          <article>
            <span>AOI</span>
            <strong>{aoiLabel}</strong>
          </article>
          <article>
            <span>图层数</span>
            <strong>{layerRows.filter((row) => row.available).length}</strong>
          </article>
        </div>

        <div className="wb-preview">
          {previewUrl ? <img alt="结果预览" src={previewUrl} /> : <p>任务完成后，这里会显示成果预览图。</p>}
        </div>
      </section>

      <section className="wb-panel">
        <div className="wb-panel-head">
          <div>
            <p className="wb-kicker">Layers</p>
            <h2>图层树</h2>
          </div>
        </div>

        <div className="wb-layer-stack">
          {layerRows.map((row) => {
            const control = layerControls[row.key];
            return (
              <article key={row.key} className={`wb-layer-row ${row.available ? "" : "wb-layer-row-disabled"}`}>
                <div className="wb-layer-row-head">
                  <label>
                    <input
                      type="checkbox"
                      checked={control.visible}
                      disabled={!row.available}
                      onChange={(event) => onUpdateLayerControl(row.key, { visible: event.target.checked })}
                    />
                    <strong>{row.title}</strong>
                    <span>{row.subtitle}</span>
                  </label>
                  <span className="wb-pill">{Math.round(control.opacity * 100)}%</span>
                </div>
                <input
                  type="range"
                  min="0"
                  max="100"
                  value={Math.round(control.opacity * 100)}
                  disabled={!row.available}
                  onChange={(event) => onUpdateLayerControl(row.key, { opacity: Number(event.target.value) / 100 })}
                />
              </article>
            );
          })}
        </div>
      </section>

      <section className="wb-panel">
        <div className="wb-panel-head">
          <div>
            <p className="wb-kicker">Exports</p>
            <h2>产物下载</h2>
          </div>
        </div>

        <div className="wb-export-stack">
          {exportCards.map((card) =>
            card.artifact ? (
              <a
                key={card.id}
                className="wb-export-row wb-export-row-ready"
                href={card.artifact.download_url}
                target="_blank"
                rel="noreferrer"
              >
                <div>
                  <strong>{card.label}</strong>
                  <span>
                    {card.artifact.mime_type} · {formatBytes(card.artifact.size_bytes)}
                  </span>
                </div>
                <span>下载</span>
              </a>
            ) : (
              <div key={card.id} className="wb-export-row wb-export-row-pending">
                <div>
                  <strong>{card.label}</strong>
                  <span>{card.subtitle}</span>
                </div>
                <span>待生成</span>
              </div>
            ),
          )}
        </div>

        <div className="wb-text-stack">
          <article>
            <h3>结果摘要</h3>
            <p>{summaryText}</p>
          </article>
          <article>
            <h3>方法说明</h3>
            <p>{methodsText}</p>
          </article>
        </div>
      </section>
    </aside>
  );
}
