from __future__ import annotations

import os
from datetime import datetime

from utils.logger import Logger


class LogSession:
    def __init__(self, run_root: str = "logs", run_id: str | None = None):
        self.run_root = run_root
        self.run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self.run_dir = os.path.join(self.run_root, self.run_id)
        self.workers_dir = os.path.join(self.run_dir, "workers")
        os.makedirs(self.workers_dir, exist_ok=True)

    def main_logger(self, *, print_to_console: bool = True) -> Logger:
        return Logger(
            name="main",
            log_dir=self.run_dir,
            print_to_console=print_to_console,
            log_filename=os.path.join(self.run_dir, "main.log"),
        )

    def worker_logger(self, worker_id: int, *, print_to_console: bool = False) -> Logger:
        filename = os.path.join(self.workers_dir, f"worker_{worker_id:03d}.log")
        return Logger(
            name=f"worker_{worker_id:03d}",
            log_dir=self.workers_dir,
            print_to_console=print_to_console,
            log_filename=filename,
        )

    def pool_state_path(self, pool_id: int = 0) -> str:
        pool_dir = os.path.join(self.run_dir, f"lemma_pool_{pool_id:02d}")
        os.makedirs(pool_dir, exist_ok=True)
        return os.path.join(pool_dir, "state.json")

