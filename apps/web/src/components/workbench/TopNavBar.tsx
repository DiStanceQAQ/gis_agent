import type { TopNavBarProps } from "./types";

export default function TopNavBar({
  theme,
  onToggleTheme,
  sessionId,
  statusLabel,
}: TopNavBarProps) {
  const compactSessionId = sessionId ? sessionId.slice(-4).toUpperCase() : "----";

  return (
    <header className="wb-topbar">
      <div className="wb-topbar-left">
        <h1 className="wb-brand-title">GEOINT 空间智能</h1>
        <nav className="wb-topbar-nav">
          <span className="wb-session-id">会话 ID: #{compactSessionId}</span>
          <span className="wb-system-state">
            <span className="wb-system-dot" />
            系统状态: {statusLabel}
          </span>
        </nav>
      </div>

      <div className="wb-topbar-right">
        <button className="wb-avatar-btn" type="button" onClick={onToggleTheme} aria-label="切换主题">
          <span className="material-symbols-outlined">{theme === "dark" ? "light_mode" : "dark_mode"}</span>
        </button>
      </div>
    </header>
  );
}
