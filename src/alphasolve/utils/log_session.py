from __future__ import annotations

import os
from datetime import datetime

from alphasolve.utils.logger import Logger


def generate_current_version() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


class LogSession:
    def __init__(self, run_root: str = "logs", progress_path: str = 'progress', run_id: str | None = None):
        self.run_root = run_root
        self.progress_path = progress_path
        self.run_id = run_id or generate_current_version() 
        self.run_dir = os.path.join(self.run_root, self.run_id)
        self.workers_dir = os.path.join(self.run_dir, "workers")
        ## 日志文件
        os.makedirs(self.workers_dir, exist_ok=True)
        ## progress 只记录一个东西, 当前的版本是哪个
        os.makedirs(self.progress_path, exist_ok=True)
        self.version_file = os.path.join(self.progress_path, "current_version")

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
        return self._pool_state_path(self.run_id, pool_id)

    def previous_state_path(self, pool_id: int = 0) -> str:
        if not os.path.exists(self.version_file): ## 第一种情况: 连 version_file 都没有
            return None
        with open(self.version_file, "r", encoding="utf-8") as f: 
            name = f.read().strip()
            if not name:  ## 第二种情况: version_file 存在, 但是里面没有内容   
                return None
            return self._pool_state_path(name, pool_id)

    def _pool_state_path(self, cur_dir, pool_id: int = 0) -> str:
        pool_dir = os.path.join(self.run_root, cur_dir, f"lemma_pool_{pool_id:02d}")
        os.makedirs(pool_dir, exist_ok=True)
        return os.path.join(pool_dir, "state.json")

    def update_version(self) -> None:
        with open(self.version_file, "w", encoding="utf-8") as f: 
            f.write(self.run_id)
            f.flush()