import type {
  MessageCreateResponse,
  OperationPlan,
  SessionMessagesResponse,
  SessionResponse,
  SessionTasksResponse,
  TaskDetail,
  TaskEvent,
  TaskEventsResponse,
  UploadedFilePreviewResponse,
  UploadResponse,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

type ApiErrorPayload = {
  error?: {
    code?: string;
    message?: string;
    detail?: unknown;
  };
};

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function translateApiError(code?: string, message?: string, detail?: unknown): string | null {
  if (code === "AOI_AREA_TOO_LARGE") {
    const detailObj = asRecord(detail);
    const area = typeof detailObj?.area_km2 === "number" ? detailObj.area_km2 : null;
    const max = typeof detailObj?.max_area_km2 === "number" ? detailObj.max_area_km2 : null;
    if (area !== null && max !== null) {
      return `研究区范围过大（${area.toFixed(1)} km²），当前上限为 ${max.toFixed(1)} km²。请缩小 AOI 后重试。`;
    }
    return "研究区范围过大，超出当前系统上限。请缩小 AOI 后重试。";
  }
  if (code === "AOI_PARSE_FAILED") {
    return "研究区解析失败，请检查上传矢量文件是否有效。";
  }
  if (code === "SESSION_NOT_FOUND") {
    return "会话不存在，请刷新页面后重试。";
  }
  if (code === "FILE_NOT_FOUND") {
    return "上传文件不存在，请重新上传。";
  }
  if (code === "INVALID_FILE_TYPE") {
    return "文件类型不受支持，请上传允许的格式。";
  }

  if (message === "AOI area is too large for the MVP workflow.") {
    return "研究区范围过大，超出当前系统上限。请缩小 AOI 后重试。";
  }
  if (message === "Session not found.") {
    return "会话不存在，请刷新页面后重试。";
  }
  if (message === "Uploaded file not found.") {
    return "上传文件不存在，请重新上传。";
  }

  return null;
}

function formatApiErrorMessage(payload: ApiErrorPayload | null, fallback?: string): string {
  const translated = translateApiError(payload?.error?.code, payload?.error?.message, payload?.error?.detail);
  if (translated) {
    return translated;
  }
  if (payload?.error?.message) {
    return payload.error.message;
  }
  if (fallback) {
    return fallback;
  }
  return "请求失败，请稍后重试。";
}

function resolveApiUrl(path: string): string {
  if (API_BASE.startsWith("http://") || API_BASE.startsWith("https://")) {
    return `${API_BASE}${path}`;
  }
  return new URL(`${API_BASE}${path}`, window.location.origin).toString();
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(resolveApiUrl(path), {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    const text = await response.text();
    const fallbackMessage = text || `请求失败（${response.status}）`;
    let payload: ApiErrorPayload | null = null;
    try {
      payload = JSON.parse(text) as ApiErrorPayload;
    } catch {
      payload = null;
    }
    throw new Error(formatApiErrorMessage(payload, fallbackMessage));
  }

  return response.json() as Promise<T>;
}

export function createSession(): Promise<SessionResponse> {
  return request<SessionResponse>("/sessions", { method: "POST" });
}

export function getSessionTasks(sessionId: string, limit = 8): Promise<SessionTasksResponse> {
  return request<SessionTasksResponse>(`/sessions/${sessionId}/tasks?limit=${limit}`);
}

export function getSessionMessages(sessionId: string, limit = 30, cursor?: string | null): Promise<SessionMessagesResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor) {
    params.set("cursor", cursor);
  }
  return request<SessionMessagesResponse>(`/sessions/${sessionId}/messages?${params.toString()}`);
}

export function createMessage(payload: {
  session_id: string;
  content: string;
  file_ids: string[];
}): Promise<MessageCreateResponse> {
  return request<MessageCreateResponse>("/messages", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

type CreateMessageStreamHandlers = {
  onDelta?: (text: string) => void;
};

type ParsedSSEEvent = {
  event: string;
  data: string;
};

function parseSSEEvent(rawEvent: string): ParsedSSEEvent | null {
  const lines = rawEvent.split("\n").map((line) => line.trimEnd());
  let eventName = "message";
  const dataLines: string[] = [];

  for (const line of lines) {
    if (!line) {
      continue;
    }
    if (line.startsWith("event:")) {
      eventName = line.slice("event:".length).trim() || "message";
      continue;
    }
    if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }

  if (!dataLines.length) {
    return null;
  }

  return {
    event: eventName,
    data: dataLines.join("\n"),
  };
}

export async function createMessageStream(
  payload: {
    session_id: string;
    content: string;
    file_ids: string[];
  },
  handlers: CreateMessageStreamHandlers = {},
): Promise<MessageCreateResponse> {
  const response = await fetch(resolveApiUrl("/messages?stream=true"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    const text = await response.text();
    const fallbackMessage = text || `请求失败（${response.status}）`;
    let parsed: ApiErrorPayload | null = null;
    try {
      parsed = JSON.parse(text) as ApiErrorPayload;
    } catch {
      parsed = null;
    }
    throw new Error(formatApiErrorMessage(parsed, fallbackMessage));
  }

  if (!response.body) {
    throw new Error("消息流为空，请稍后重试。");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalMessage: MessageCreateResponse | null = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }

    buffer += decoder.decode(value, { stream: true });
    let separatorIndex = buffer.indexOf("\n\n");

    while (separatorIndex !== -1) {
      const rawEvent = buffer.slice(0, separatorIndex);
      buffer = buffer.slice(separatorIndex + 2);

      const parsedEvent = parseSSEEvent(rawEvent);
      if (parsedEvent) {
        if (parsedEvent.event === "delta") {
          try {
            const payloadData = JSON.parse(parsedEvent.data) as { text?: string };
            if (payloadData.text) {
              handlers.onDelta?.(payloadData.text);
            }
          } catch {
            // Ignore malformed delta payload.
          }
        } else if (parsedEvent.event === "message") {
          try {
            finalMessage = JSON.parse(parsedEvent.data) as MessageCreateResponse;
          } catch {
            // Ignore malformed final message payload.
          }
        } else if (parsedEvent.event === "error") {
          try {
            const payloadData = JSON.parse(parsedEvent.data) as { message?: string; code?: string; detail?: unknown };
            const translated = translateApiError(payloadData.code, payloadData.message, payloadData.detail);
            throw new Error(translated || payloadData.message || "消息流处理失败。");
          } catch (error) {
            if (error instanceof Error) {
              throw error;
            }
            throw new Error("消息流处理失败。");
          }
        }
      }

      separatorIndex = buffer.indexOf("\n\n");
    }
  }

  if (!finalMessage) {
    throw new Error("消息流未返回最终结果。请稍后重试。");
  }
  return finalMessage;
}

export function getTask(taskId: string): Promise<TaskDetail> {
  return request<TaskDetail>(`/tasks/${taskId}`);
}

export function getTaskEvents(taskId: string, sinceId = 0): Promise<TaskEventsResponse> {
  const query = sinceId > 0 ? `?since_id=${sinceId}` : "";
  return request<TaskEventsResponse>(`/tasks/${taskId}/events${query}`);
}

export function rerunTask(taskId: string, override: Record<string, unknown>): Promise<TaskDetail> {
  return request<TaskDetail>(`/tasks/${taskId}/rerun`, {
    method: "POST",
    body: JSON.stringify({ override }),
  });
}

export function patchTaskPlanDraft(taskId: string, operationPlan: OperationPlan): Promise<TaskDetail> {
  return request<TaskDetail>(`/tasks/${taskId}/plan`, {
    method: "PATCH",
    body: JSON.stringify({ operation_plan: operationPlan }),
  });
}

export function approveTaskPlan(taskId: string, approvedVersion: number): Promise<TaskDetail> {
  return request<TaskDetail>(`/tasks/${taskId}/approve`, {
    method: "POST",
    body: JSON.stringify({ approved_version: approvedVersion }),
  });
}

export function rejectTaskPlan(taskId: string, reason?: string): Promise<TaskDetail> {
  return request<TaskDetail>(`/tasks/${taskId}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export async function uploadFile(sessionId: string, file: File): Promise<UploadResponse> {
  const formData = new FormData();
  formData.append("session_id", sessionId);
  formData.append("file", file);
  const response = await fetch(resolveApiUrl("/files"), {
    method: "POST",
    body: formData,
  });
  if (!response.ok) {
    const text = await response.text();
    const fallbackMessage = text || `上传失败（${response.status}）`;
    let payload: ApiErrorPayload | null = null;
    try {
      payload = JSON.parse(text) as ApiErrorPayload;
    } catch {
      payload = null;
    }
    throw new Error(formatApiErrorMessage(payload, fallbackMessage));
  }
  return response.json() as Promise<UploadResponse>;
}

export function getFilePreview(fileId: string): Promise<UploadedFilePreviewResponse> {
  return request<UploadedFilePreviewResponse>(`/files/${fileId}/preview`);
}

export type TaskEventStreamHandlers = {
  onEvent: (event: TaskEvent) => void;
  onError?: (error: Event | Error) => void;
};

export function openTaskEventsStream(taskId: string, sinceId: number, handlers: TaskEventStreamHandlers): () => void {
  if (typeof window === "undefined" || typeof EventSource === "undefined") {
    handlers.onError?.(new Error("EventSource is not available in current runtime."));
    return () => {};
  }

  const url = new URL(resolveApiUrl(`/tasks/${taskId}/events/stream`));
  url.searchParams.set("since_id", String(Math.max(0, sinceId)));

  const stream = new EventSource(url.toString());

  const handleEvent = (event: MessageEvent<string>) => {
    try {
      const payload = JSON.parse(event.data) as TaskEvent;
      handlers.onEvent(payload);
    } catch {
      // Ignore malformed stream payloads and keep stream alive.
    }
  };

  stream.addEventListener("task_event", handleEvent as EventListener);
  stream.onmessage = handleEvent;
  stream.onerror = (error) => {
    handlers.onError?.(error);
    stream.close();
  };

  return () => {
    stream.removeEventListener("task_event", handleEvent as EventListener);
    stream.close();
  };
}
