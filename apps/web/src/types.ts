export type SessionResponse = {
  session_id: string;
  status: string;
};

export type UploadResponse = {
  file_id: string;
  file_type: string;
  storage_key: string;
  original_name: string;
};

export type MessageCreateResponse = {
  message_id: string;
  task_id: string;
  task_status: string;
  need_clarification: boolean;
  missing_fields: string[];
};

export type Recommendation = {
  primary_dataset: string;
  backup_dataset?: string | null;
  reason: string;
  risk_note?: string | null;
};

export type Candidate = {
  dataset_name: string;
  scene_count: number;
  coverage_ratio: number;
  effective_pixel_ratio_estimate: number;
  spatial_resolution: number;
  temporal_density_note: string;
  suitability_score: number;
  recommendation_rank: number;
};

export type TaskStep = {
  step_name: string;
  status: string;
  started_at?: string | null;
  ended_at?: string | null;
  detail?: Record<string, unknown> | null;
};

export type Artifact = {
  artifact_id: string;
  artifact_type: string;
  mime_type: string;
  size_bytes: number;
  download_url: string;
};

export type TaskDetail = {
  task_id: string;
  status: string;
  current_step?: string | null;
  analysis_type: string;
  task_spec?: Record<string, unknown> | null;
  recommendation?: Recommendation | null;
  candidates: Candidate[];
  steps: TaskStep[];
  artifacts: Artifact[];
  summary_text?: string | null;
  methods_text?: string | null;
  png_preview_url?: string | null;
  requested_time_range?: Record<string, string> | null;
  actual_time_range?: Record<string, string> | null;
  error_message?: string | null;
};

