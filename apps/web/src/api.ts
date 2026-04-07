import type {
  MessageCreateResponse,
  OperationPlan,
  SessionResponse,
  SessionTasksResponse,
  TaskDetail,
  TaskEvent,
  TaskEventsResponse,
  UploadResponse,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

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
    try {
      const payload = JSON.parse(text) as {
        error?: {
          code?: string;
          message?: string;
          detail?: unknown;
        };
      };
      if (payload.error?.message) {
        throw new Error(payload.error.message);
      }
    } catch {
      // Fall through to raw text message below.
    }
    throw new Error(text || `Request failed with ${response.status}`);
  }

  return response.json() as Promise<T>;
}

export function createSession(): Promise<SessionResponse> {
  return request<SessionResponse>("/sessions", { method: "POST" });
}

export function getSessionTasks(sessionId: string, limit = 8): Promise<SessionTasksResponse> {
  return request<SessionTasksResponse>(`/sessions/${sessionId}/tasks?limit=${limit}`);
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
    try {
      const payload = JSON.parse(text) as { error?: { message?: string } };
      if (payload.error?.message) {
        throw new Error(payload.error.message);
      }
    } catch {
      // Fall through to raw text message below.
    }
    throw new Error(text || `Upload failed with ${response.status}`);
  }
  return response.json() as Promise<UploadResponse>;
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
