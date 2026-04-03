export type SessionResponse = {
  session_id: string;
  status: string;
};

export type SessionTask = {
  task_id: string;
  parent_task_id?: string | null;
  status: string;
  current_step?: string | null;
  analysis_type: string;
  created_at?: string | null;
  task_spec?: TaskSpec | null;
};

export type SessionTasksResponse = {
  session_id: string;
  tasks: SessionTask[];
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
  clarification_message?: string | null;
};

export type Recommendation = {
  primary_dataset: string;
  backup_dataset?: string | null;
  scores?: Record<string, number>;
  reason: string;
  risk_note?: string | null;
  confidence?: number | null;
  error_code?: string | null;
  error_message?: string | null;
};

export type TimeRange = {
  start: string;
  end: string;
};

export type TaskSpec = {
  aoi_input?: string | null;
  aoi_source_type?: string | null;
  time_range?: TimeRange | null;
  requested_dataset?: string | null;
  analysis_type?: string;
  preferred_output?: string[];
  user_priority?: string;
  need_confirmation?: boolean;
  missing_fields?: string[];
  clarification_message?: string | null;
  created_from?: string | null;
};

export type Candidate = {
  dataset_name: string;
  collection_id: string;
  scene_count: number;
  coverage_ratio: number;
  effective_pixel_ratio_estimate: number;
  cloud_metric_summary?: Record<string, number> | null;
  spatial_resolution: number;
  temporal_density_note: string;
  suitability_score: number;
  recommendation_rank: number;
  summary_json?: Record<string, unknown> | null;
};

export type TaskStep = {
  step_name: string;
  status: string;
  started_at?: string | null;
  ended_at?: string | null;
  observation?: Record<string, unknown> | null;
  detail?: Record<string, unknown> | null;
};

export type TaskPlanStep = {
  step_name: string;
  tool_name: string;
  title: string;
  purpose: string;
  status: string;
  reasoning?: string | null;
  depends_on: string[];
  detail?: Record<string, unknown> | null;
};

export type TaskPlan = {
  version: string;
  mode: string;
  status: string;
  objective: string;
  reasoning_summary: string;
  missing_fields: string[];
  steps: TaskPlanStep[];
  error_code?: string | null;
  error_message?: string | null;
};

export type OperationNode = {
  step_id: string;
  op_name: string;
  depends_on: string[];
  inputs: Record<string, string>;
  params: Record<string, unknown>;
  outputs: Record<string, string>;
  retry_policy?: Record<string, number>;
};

export type OperationPlan = {
  version: number;
  status: "draft" | "validated" | "approved";
  missing_fields: string[];
  nodes: OperationNode[];
};

export type TaskEvent = {
  event_id: number;
  event_type: string;
  step_name?: string | null;
  status?: string | null;
  created_at: string;
  detail?: Record<string, unknown> | null;
};

export type TaskEventsResponse = {
  task_id: string;
  status: string;
  current_step?: string | null;
  next_cursor: number;
  events: TaskEvent[];
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
  parent_task_id?: string | null;
  status: string;
  current_step?: string | null;
  analysis_type: string;
  created_at?: string | null;
  task_spec?: TaskSpec | null;
  task_plan?: TaskPlan | null;
  operation_plan?: OperationPlan | null;
  recommendation?: Recommendation | null;
  candidates: Candidate[];
  steps: TaskStep[];
  artifacts: Artifact[];
  summary_text?: string | null;
  methods_text?: string | null;
  png_preview_url?: string | null;
  aoi_name?: string | null;
  aoi_bbox_bounds?: number[] | null;
  aoi_area_km2?: number | null;
  requested_time_range?: TimeRange | null;
  actual_time_range?: TimeRange | null;
  error_code?: string | null;
  error_message?: string | null;
  clarification_message?: string | null;
};
