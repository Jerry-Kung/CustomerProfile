from __future__ import annotations


class TaskQueueFullError(Exception):
    def __init__(self, *, max_waiting: int, queued_count: int):
        self.max_waiting = max_waiting
        self.queued_count = queued_count
        super().__init__(f"task queue is full: queued={queued_count}, max_waiting={max_waiting}")
