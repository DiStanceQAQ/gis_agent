import type { TopNavBarProps } from "./types";

export default function TopNavBar({
  theme,
  onToggleTheme,
  sessionId,
  taskId,
  statusTone,
  statusLabel,
  datasetLabel,
  statusText,
}: TopNavBarProps) {
  return (
    <header className="wb-topbar">
      <div className="wb-brand-zone">
        <h1>GEOINT 空间智能</h1>
        <p>Digital Meridian Workbench</p>
      </div>

      <div className="wb-status-zone">
        <div className="wb-status-row">
          <span className={`wb-badge wb-badge-${statusTone}`}>{statusLabel}</span>
          <span className="wb-chip">Task {taskId ?? "--"}</span>
          <span className="wb-chip">Session {sessionId ?? "初始化中"}</span>
          <span className="wb-chip">{datasetLabel}</span>
        </div>
        <p>{statusText}</p>
      </div>

      <div className="wb-topbar-actions">
        <button className="wb-ghost-btn" type="button" onClick={onToggleTheme}>
          {theme === "dark" ? "浅色模式" : "深色模式"}
        </button>
      </div>
    </header>
  );
}
