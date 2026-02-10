from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

LEVEL_SYMBOLS = {
    'DEBUG': '🔍',
    'INFO': '📝',
    'WARNING': '⚠️',
    'ERROR': '❌',
    'CRITICAL': '🔥',
    'SUCCESS': '✅',
    'METRIC': '📊',
}


class Logger:
    def __init__(
        self,
        name: str = "AlphaSolve",
        *,
        log_dir: str = "logs",
        print_to_console: bool = True,
        timestamp: Optional[str] = None,
    ) -> None:
        self.name = name
        self.log_dir = log_dir
        self.print_to_console_default = print_to_console
        self.timestamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self.log_filename = os.path.join(self.log_dir, f"{self.name}_" + f"{self.timestamp}.log")
        self._streaming_open = False

        os.makedirs(self.log_dir, exist_ok=True)

        logger_name = f"tid_{name}_timestamp_{self.timestamp}"
        self._logger = logging.getLogger(logger_name)
        self._logger.setLevel(logging.DEBUG)
        if self._logger.handlers:
            for handler in self._logger.handlers[:]:
                handler.close()
                self._logger.removeHandler(handler)

        file_formatter = logging.Formatter(
            '%(asctime)s.%(msecs)03d │ %(levelname)-8s │ %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        )
        console_formatter = logging.Formatter('%(message)s')

        file_handler = logging.FileHandler(self.log_filename, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(file_formatter)
        self._logger.addHandler(file_handler)
        # Keep a reference so we can append raw streaming content (no prefix, no extra newline)
        # directly to the same underlying stream without introducing a second file handle.
        self._file_handler = file_handler
        self._write_log_header()

    def get_log_filename(self) -> str:
        return self.log_filename

    def log_print(
        self,
        *args: object,
        sep: str = ' ',
        end: str = '\n',
        print_to_console: Optional[bool] = None,
        level: str = 'INFO',
        module: Optional[str] = None,
    ) -> None:
        if print_to_console is None:
            print_to_console = self.print_to_console_default

        message = sep.join(str(arg) for arg in args)

        if print_to_console:
            print(message, end=end, flush=True)

        # Streaming mode: when caller uses end != "\n" (typically end=""), we treat it as
        # incremental output and append to the log file without the timestamp/level prefix.
        # This prevents one-prefix-per-fragment while still preserving the exact stream.
        if end != '\n' or (message == '' and end == '\n'):
            try:
                self._file_handler.stream.write(message + end)
                self._file_handler.flush()
                self._streaming_open = not str(end).endswith('\n')
                return
            except Exception:
                # Fall back to normal logging path if direct stream write fails.
                # (e.g., file handler not initialized or stream closed)
                pass

        # If we previously wrote streaming fragments without a newline, ensure the next
        # prefixed log entry starts on a new line.
        if self._streaming_open:
            try:
                self._file_handler.stream.write('\n')
                self._file_handler.flush()
            except Exception:
                pass
            self._streaming_open = False

        if not message.strip():
            return

        level = level.upper()
        formatted_msg = self._format_message(message, level, module)

        if level in ('SUCCESS', 'METRIC'):
            self._logger.info(formatted_msg)
        elif level == 'DEBUG':
            self._logger.debug(formatted_msg)
        elif level == 'WARNING':
            self._logger.warning(formatted_msg)
        elif level == 'ERROR':
            self._logger.error(formatted_msg)
        elif level == 'CRITICAL':
            self._logger.critical(formatted_msg)
        else:
            self._logger.info(formatted_msg)

    def log_separator(self, style: str = 'line', width: int = 100, print_to_console: Optional[bool] = None) -> None:
        styles = {
            'line': '─' * width,
            'double': '═' * width,
            'dash': '┈' * width,
            'dot': '·' * width,
            'section': '━' * width,
        }
        separator = styles.get(style, '─' * width)
        self.log_print(separator, print_to_console=print_to_console, level='INFO')

    def log_section(self, title: str, width: int = 100, print_to_console: Optional[bool] = None) -> None:
        self.log_separator('section', width, print_to_console)
        centered_title = f"  {title}  "
        padding = (width - len(centered_title)) // 2
        formatted_title = '│' + ' ' * padding + centered_title + ' ' * (width - padding - len(centered_title) - 1) + '│'
        self.log_print(formatted_title, print_to_console=print_to_console, level='INFO')
        self.log_separator('section', width, print_to_console)

    def log_box(self, message: str, width: int = 100, print_to_console: Optional[bool] = None, level: str = 'INFO') -> None:
        lines = message.split('\n')
        self.log_print('┌' + '─' * (width - 2) + '┐', print_to_console=print_to_console, level=level)
        for line in lines:
            padded_line = line + ' ' * (width - len(line) - 4)
            self.log_print(f'│ {padded_line} │', print_to_console=print_to_console, level=level)
        self.log_print('└' + '─' * (width - 2) + '┘', print_to_console=print_to_console, level=level)

    def log_metric(self, metric_name: str, value, unit: str = '', print_to_console: Optional[bool] = None) -> None:
        formatted_value = f"{value}{unit}" if unit else str(value)
        message = f"{metric_name}: {formatted_value}"
        self.log_print(message, print_to_console=print_to_console, level='METRIC')

    def log_dict(
        self,
        data: dict,
        title: Optional[str] = None,
        print_to_console: Optional[bool] = None,
        level: str = 'INFO',
    ) -> None:
        if title:
            self.log_print(f"┌─ {title}", print_to_console=print_to_console, level=level)
        for key, value in data.items():
            self.log_print(f"│ {key}: {value}", print_to_console=print_to_console, level=level)
        if title:
            self.log_print("└─", print_to_console=print_to_console, level=level)

    def debug(self, *args: object, **kwargs) -> None:
        kwargs.setdefault('level', 'DEBUG')
        self.log_print(*args, **kwargs)

    def info(self, *args: object, **kwargs) -> None:
        kwargs.setdefault('level', 'INFO')
        self.log_print(*args, **kwargs)

    def warning(self, *args: object, **kwargs) -> None:
        kwargs.setdefault('level', 'WARNING')
        self.log_print(*args, **kwargs)

    def error(self, *args: object, **kwargs) -> None:
        kwargs.setdefault('level', 'ERROR')
        self.log_print(*args, **kwargs)

    def critical(self, *args: object, **kwargs) -> None:
        kwargs.setdefault('level', 'CRITICAL')
        self.log_print(*args, **kwargs)

    def success(self, *args: object, **kwargs) -> None:
        kwargs.setdefault('level', 'SUCCESS')
        self.log_print(*args, **kwargs)

    def metric(self, *args: object, **kwargs) -> None:
        kwargs.setdefault('level', 'METRIC')
        self.log_print(*args, **kwargs)

    def close(self) -> None:
        for handler in self._logger.handlers[:]:
            handler.close()
            self._logger.removeHandler(handler)

    def _write_log_header(self) -> None:
        header = f"""
{'='*100}
{'AlphaSolve 日志系统':^100}
{'='*100}
启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}
日志文件: {self.timestamp}.log
{'='*100}
"""
        for handler in self._logger.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.stream.write(header + '\n')
                handler.flush()

    def _format_message(self, message: str, level: str, module: Optional[str]) -> str:
        symbol = LEVEL_SYMBOLS.get(level.upper(), '📝')
        if module:
            return f"{symbol} [{module}] {message}"
        if message.strip().startswith('[') and ']' in message:
            return f"{symbol} {message}"
        return f"{symbol} {message}"


__all__ = ["Logger"]
