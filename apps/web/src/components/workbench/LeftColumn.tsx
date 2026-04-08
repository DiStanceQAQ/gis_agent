import type { LeftColumnProps } from "./types";

type LayerManagerRow = {
  id: string;
  title: string;
  subtitle: string;
  available: boolean;
  visible: boolean;
  opacity: number;
  isLoading?: boolean;
  onLocate?: (() => void) | null;
  onRemove?: (() => void) | null;
  onUpdate: (nextValue: { visible?: boolean; opacity?: number }) => void;
};

const DEFAULT_LAYER_CONTROL = {
  visible: true,
  opacity: 1,
};

export default function LeftColumn({
  uploadLayerControls,
  artifactLayerControls,
  uploadLayerRows,
  artifactLayerRows,
  isUploading,
  uploadError,
  onUploadFile,
  onUpdateUploadLayerControl,
  onUpdateArtifactLayerControl,
  onLocateUploadLayer,
  onLocateArtifactLayer,
  onRemoveUploadLayer,
}: LeftColumnProps) {
  const renderLayerManagerRow = (row: LayerManagerRow) => (
    <article key={row.id} className={`wb-layer-manager-row ${row.available ? "" : "is-disabled"}`}>
      <div className="wb-layer-manager-head">
        <div className="wb-layer-title-wrap">
          <span>{row.title}</span>
        </div>

        <div className="wb-layer-head-actions">
          {row.isLoading ? (
            <span className="wb-layer-loading" aria-label="正在生成预览">
              <span className="material-symbols-outlined wb-icon-spin">autorenew</span>
              处理中
            </span>
          ) : row.onLocate ? (
            <button type="button" className="wb-layer-locate-btn" onClick={row.onLocate} disabled={!row.available}>
              <span className="material-symbols-outlined">my_location</span>
              定位
            </button>
          ) : null}

          {row.onRemove ? (
            <button type="button" className="wb-layer-remove-btn" onClick={row.onRemove}>
              <span className="material-symbols-outlined">delete</span>
              移除
            </button>
          ) : null}
        </div>
      </div>

      <p className="wb-layer-row-subtitle">{row.subtitle}</p>

      <div className="wb-layer-row-actions">
        <label>
          <input
            type="checkbox"
            checked={row.visible}
            disabled={!row.available}
            onChange={(event) => row.onUpdate({ visible: event.target.checked })}
          />
          显示
        </label>
        <span>{Math.round(row.opacity * 100)}%</span>
      </div>

      <input
        type="range"
        min="0"
        max="100"
        value={Math.round(row.opacity * 100)}
        disabled={!row.available}
        onChange={(event) => row.onUpdate({ opacity: Number(event.target.value) / 100 })}
      />
    </article>
  );

  return (
    <aside className="wb-left-floating">
      <section className="wb-layer-card">
        <div className="wb-layer-card-head">
          <h3>
            图层管理
            <span className="material-symbols-outlined">filter_list</span>
          </h3>

          <label className="wb-upload-source-btn wb-upload-source-btn-inline">
            <input type="file" multiple accept=".tif,.tiff,.img,.vrt,.geojson,.json,.zip,.shp,.gpkg,.kml,.kmz" onChange={onUploadFile} />
            <span className="material-symbols-outlined">upload_file</span>
            {isUploading ? "上传中..." : "上传"}
          </label>
        </div>

        {uploadError ? <p className="wb-source-error">{uploadError}</p> : null}

        <div className="wb-layer-group">
          <p className="wb-layer-group-title">上传数据</p>
          <div className="wb-layer-manager-list">
            {uploadLayerRows.length ? (
              uploadLayerRows.map((row) => {
                const control = uploadLayerControls[row.fileId] ?? DEFAULT_LAYER_CONTROL;
                const isReady = row.status === "ready";
                const uploadAvailable = isReady && row.mapSupported;
                let uploadSubtitle = row.subtitle;
                if (row.status === "pending") {
                  uploadSubtitle = `${row.subtitle} · 预览生成中...`;
                } else if (row.status === "failed") {
                  uploadSubtitle = `${row.subtitle} · 预览失败，请重传文件`;
                } else if (!uploadAvailable) {
                  uploadSubtitle = `${row.subtitle} · 当前格式暂不支持地图预览`;
                }

                return renderLayerManagerRow({
                  id: `upload-${row.fileId}`,
                  title: row.title,
                  subtitle: uploadSubtitle,
                  available: uploadAvailable,
                  visible: control.visible,
                  opacity: control.opacity,
                  isLoading: row.status === "pending",
                  onLocate: uploadAvailable ? () => onLocateUploadLayer(row.fileId) : null,
                  onRemove: () => onRemoveUploadLayer(row.fileId),
                  onUpdate: (nextValue) => onUpdateUploadLayerControl(row.fileId, nextValue),
                });
              })
            ) : (
              <p className="wb-source-empty">还没有上传图层。</p>
            )}
          </div>
        </div>

        <div className="wb-layer-group">
          <p className="wb-layer-group-title">助手产出</p>
          <div className="wb-layer-manager-list">
            {artifactLayerRows.length ? (
              artifactLayerRows.map((row) => {
                const control = artifactLayerControls[row.artifactId] ?? DEFAULT_LAYER_CONTROL;
                return renderLayerManagerRow({
                  id: `artifact-${row.artifactId}`,
                  title: row.title,
                  subtitle: row.mapSupported ? row.subtitle : `${row.subtitle} · 当前类型暂不支持地图预览`,
                  available: row.mapSupported,
                  visible: control.visible,
                  opacity: control.opacity,
                  onLocate: row.mapSupported ? () => onLocateArtifactLayer(row.artifactId) : null,
                  onUpdate: (nextValue) => onUpdateArtifactLayerControl(row.artifactId, nextValue),
                });
              })
            ) : (
              <p className="wb-source-empty">助手还没有产出可显示数据。</p>
            )}
          </div>
        </div>
      </section>
    </aside>
  );
}
