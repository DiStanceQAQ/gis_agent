import { ChangeEvent, FormEvent } from "react";
import type {
  LayerControlMap,
  LayerControlState,
  LayerKey,
  TaskHistoryEntry,
  ThemeMode,
} from "../../hooks/useWorkbenchState";
import type { Artifact, Candidate, TaskDetail, TaskSpec, TaskStep, TimeRange, UploadResponse } from "../../types";

export type StatusTone = "success" | "failed" | "running" | "queued" | "warning" | "idle";

export type LayerRow = {
  key: LayerKey;
  title: string;
  subtitle: string;
  available: boolean;
};

export type ExportCard = {
  id: string;
  label: string;
  subtitle: string;
  artifact?: Artifact;
};

export type RerunAction = {
  id: string;
  label: string;
  description: string;
  override: Record<string, unknown>;
};

export type TopNavBarProps = {
  theme: ThemeMode;
  onToggleTheme: () => void;
  sessionId: string | null;
  taskId: string | null;
  statusTone: StatusTone;
  statusLabel: string;
  datasetLabel: string;
  statusText: string;
};

export type LeftColumnProps = {
  hasResult: boolean;
  datasetLabel: string;
  timeRangeLabel: string;
  aoiLabel: string;
  layerRows: LayerRow[];
  layerControls: LayerControlMap;
  onUpdateLayerControl: (layer: LayerKey, nextValue: Partial<LayerControlState>) => void;
  exportCards: ExportCard[];
  summaryText: string;
  methodsText: string;
  previewUrl: string | null;
  formatBytes: (sizeBytes: number) => string;
};

export type RightColumnProps = {
  currentTask: TaskDetail | null;
  currentHistory: TaskHistoryEntry[];
  taskEventsCount: number;
  composerText: string;
  setComposerText: (value: string) => void;
  quickPrompts: string[];
  followupPrompts: string[];
  uploads: UploadResponse[];
  bootError: string | null;
  notice: string | null;
  uploadError: string | null;
  submitError: string | null;
  planError: string | null;
  isUploading: boolean;
  isSubmitting: boolean;
  isSavingPlan: boolean;
  isApprovingPlan: boolean;
  activeActionId: string | null;
  planDraftText: string;
  setPlanDraftText: (value: string) => void;
  rerunActions: RerunAction[];
  overviewItems: Array<[string, string]>;
  handleSubmit: (event: FormEvent<HTMLFormElement>) => Promise<void>;
  handleFileChange: (event: ChangeEvent<HTMLInputElement>) => Promise<void>;
  applyPrompt: (prompt: string) => void;
  handleSavePlanDraft: () => Promise<void>;
  handleApprovePlan: () => Promise<void>;
  handleRerun: (actionId: string, override: Record<string, unknown>) => Promise<void>;
  handleHistorySelect: (taskId: string) => void;
  canEditOperationPlan: boolean;
  canApproveOperationPlan: boolean;
  formatStatusLabel: (status?: string | null) => string;
  formatStatusTone: (status?: string | null) => StatusTone;
  formatDateTime: (value?: string | null) => string;
  formatDatasetName: (dataset?: string | null) => string;
  formatPercent: (value?: number | null) => string;
  formatCloudPercent: (value?: number | null) => string;
  formatSourceLabel: (source?: unknown) => string;
  getDetailEntries: (detail?: Record<string, unknown> | null) => Array<[string, string]>;
};

export type WorkbenchViewModel = {
  task: TaskDetail | null;
  taskSpec?: TaskSpec | null;
  timeRange?: TimeRange | null;
  steps: TaskStep[];
  candidates: Candidate[];
};
