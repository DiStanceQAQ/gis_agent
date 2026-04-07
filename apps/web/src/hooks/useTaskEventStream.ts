import { useEffect, useRef } from "react";
import { openTaskEventsStream } from "../api";
import type { TaskEvent } from "../types";

type UseTaskEventStreamOptions = {
  taskId: string | null;
  sinceId: number;
  enabled: boolean;
  onEvent: (event: TaskEvent) => void;
  onError?: (error: Event | Error) => void;
};

export function useTaskEventStream(options: UseTaskEventStreamOptions): void {
  const onEventRef = useRef(options.onEvent);
  const onErrorRef = useRef(options.onError);

  onEventRef.current = options.onEvent;
  onErrorRef.current = options.onError;

  useEffect(() => {
    if (!options.enabled || !options.taskId) {
      return;
    }

    return openTaskEventsStream(options.taskId, options.sinceId, {
      onEvent: (event) => onEventRef.current(event),
      onError: (error) => onErrorRef.current?.(error),
    });
  }, [options.enabled, options.taskId]);
}
