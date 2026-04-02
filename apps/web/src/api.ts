import type {
  MessageCreateResponse,
  SessionResponse,
  SessionTasksResponse,
  TaskDetail,
  TaskEventsResponse,
  UploadResponse,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
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

export async function uploadFile(sessionId: string, file: File): Promise<UploadResponse> {
  const formData = new FormData();
  formData.append("session_id", sessionId);
  formData.append("file", file);
  const response = await fetch(`${API_BASE}/files`, {
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
