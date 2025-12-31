import logging
import os
from datetime import datetime
import sys
import threading

# Thread-local storage for logger instances
_thread_local = threading.local()


def get_logger(name: str = "AlphaSolve", print_to_console: bool = True):
    """
    获取或创建一个logger实例
    
    每个进程/线程第一次调用时会创建一个新的日志文件（带时间戳，精确到毫秒）
    后续调用返回同一个logger实例
    
    Args:
        name: logger名称
        print_to_console: 是否同时输出到控制台
    
    Returns:
        logging.Logger实例
    """
    # Check if logger already exists in thread-local storage
    if hasattr(_thread_local, 'logger') and _thread_local.logger is not None:
        return _thread_local.logger
    
    # Create logs directory if it doesn't exist
    logs_dir = "logs"
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir)
    
    # Generate timestamp with milliseconds for unique log filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]  # Remove last 3 digits to get milliseconds
    log_filename = os.path.join(logs_dir, f"{timestamp}.log")
    
    # Create logger
    logger = logging.getLogger(f"{name}_{timestamp}")
    logger.setLevel(logging.DEBUG)
    
    # Prevent adding duplicate handlers
    if logger.handlers:
        logger.handlers.clear()
    
    # Create formatters
    file_formatter = logging.Formatter(
        '[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_formatter = logging.Formatter('%(message)s')
    
    # File handler - always write to file
    file_handler = logging.FileHandler(log_filename, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)
    
    # Console handler - optional
    if print_to_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
    
    # Store in thread-local storage
    _thread_local.logger = logger
    _thread_local.log_filename = log_filename
    
    return logger


def get_log_filename():
    """获取当前logger的日志文件名"""
    if hasattr(_thread_local, 'log_filename'):
        return _thread_local.log_filename
    return None


def log_print(*args, sep=' ', end='\n', print_to_console: bool = True, level: str = 'INFO'):
    """
    类似print的日志记录函数，同时记录到日志文件和控制台
    
    Args:
        *args: 要打印的内容
        sep: 分隔符
        end: 结束符
        print_to_console: 是否输出到控制台
        level: 日志级别 ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
    """
    logger = get_logger(print_to_console=print_to_console)
    
    # Join all arguments with separator
    message = sep.join(str(arg) for arg in args)
    
    # Add end character if it's not a newline (newline is default in logging)
    if end != '\n':
        message += end
    
    # Log based on level
    level = level.upper()
    if level == 'DEBUG':
        logger.debug(message)
    elif level == 'WARNING':
        logger.warning(message)
    elif level == 'ERROR':
        logger.error(message)
    elif level == 'CRITICAL':
        logger.critical(message)
    else:  # INFO or default
        logger.info(message)


def reset_logger():
    """重置logger实例，用于创建新的日志文件"""
    if hasattr(_thread_local, 'logger'):
        # Close all handlers
        for handler in _thread_local.logger.handlers[:]:
            handler.close()
            _thread_local.logger.removeHandler(handler)
        _thread_local.logger = None
        _thread_local.log_filename = None


# Convenience functions matching different log levels
def debug(*args, **kwargs):
    """记录DEBUG级别日志"""
    log_print(*args, **kwargs, level='DEBUG')


def info(*args, **kwargs):
    """记录INFO级别日志"""
    log_print(*args, **kwargs, level='INFO')


def warning(*args, **kwargs):
    """记录WARNING级别日志"""
    log_print(*args, **kwargs, level='WARNING')


def error(*args, **kwargs):
    """记录ERROR级别日志"""
    log_print(*args, **kwargs, level='ERROR')


def critical(*args, **kwargs):
    """记录CRITICAL级别日志"""
    log_print(*args, **kwargs, level='CRITICAL')
