import { ChangeEvent, FormEvent } from "react";
import type {
  LayerControlState,
  TaskHistoryEntry,
  ThemeMode,
} from "../../hooks/useWorkbenchState";
import type {
  Artifact,
  MessageUnderstanding,
  ResponseMode,
  SessionMessage,
  TaskDetail,
  UnderstandingResponsePayload,
} from "../../types";

export type StatusTone = "success" | "failed" | "running" | "queued" | "warning" | "idle";

export type UploadLayerRow = {
  fileId: string;
  title: string;
  subtitle: string;
  mapSupported: boolean;
  status: "pending" | "ready" | "unsupported" | "failed";
};

export type ArtifactLayerRow = {
  artifactId: string;
  title: string;
  subtitle: string;
  mapSupported: boolean;
};

export type TopNavBarProps = {
  theme: ThemeMode;
  onToggleTheme: () => void;
  sessionId: string | null;
  statusLabel: string;
};

export type LeftColumnProps = {
  uploadLayerControls: Record<string, LayerControlState>;
  artifactLayerControls: Record<string, LayerControlState>;
  uploadLayerRows: UploadLayerRow[];
  artifactLayerRows: ArtifactLayerRow[];
  isUploading: boolean;
  uploadError: string | null;
  onUploadFile: (event: ChangeEvent<HTMLInputElement>) => Promise<void>;
  onUpdateUploadLayerControl: (fileId: string, nextValue: Partial<LayerControlState>) => void;
  onUpdateArtifactLayerControl: (artifactId: string, nextValue: Partial<LayerControlState>) => void;
  onLocateUploadLayer: (fileId: string) => void;
  onLocateArtifactLayer: (artifactId: string) => void;
  onRemoveUploadLayer: (fileId: string) => void;
};

export type RightColumnProps = {
  currentTaskId: string | null;
  taskHistory: TaskHistoryEntry[];
  currentTask: TaskDetail | null;
  latestResponseMode: ResponseMode | null;
  latestUnderstanding: MessageUnderstanding | null;
  latestResponsePayload: UnderstandingResponsePayload | null;
  sessionMessages: SessionMessage[];
  streamingAssistantMessage: string | null;
  messagesNextCursor: string | null;
  isLoadingMessages: boolean;
  messagesError: string | null;
  streamMode: "streaming" | "fallback_polling";
  composerText: string;
  setComposerText: (value: string) => void;
  submitError: string | null;
  notice: string | null;
  planError: string | null;
  isSubmitting: boolean;
  isSavingPlan: boolean;
  isApprovingPlan: boolean;
  isRejectingPlan: boolean;
  planDraftText: string;
  setPlanDraftText: (value: string) => void;
  handleSubmit: (event: FormEvent<HTMLFormElement>) => Promise<void>;
  handleSavePlanDraft: () => Promise<void>;
  handleApprovePlan: () => Promise<void>;
  handleRejectPlan: (reason?: string) => Promise<void>;
  handleLoadMoreMessages: () => Promise<void>;
  onHistorySelect: (taskId: string) => void;
  canEditOperationPlan: boolean;
  canApproveOperationPlan: boolean;
  formatStatusLabel: (status?: string | null) => string;
  formatStatusTone: (status?: string | null) => StatusTone;
  formatDateTime: (value?: string | null) => string;
};
