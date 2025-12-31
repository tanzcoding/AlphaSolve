import logging
import os
from datetime import datetime
import sys
import threading

# Thread-local storage for logger instances
_thread_local = threading.local()

# ANSI颜色代码（用于文件中的视觉标识，不是真正的颜色）
LEVEL_SYMBOLS = {
    'DEBUG': '🔍',
    'INFO': '📝',
    'WARNING': '⚠️',
    'ERROR': '❌',
    'CRITICAL': '🔥',
    'SUCCESS': '✅',
    'METRIC': '📊',
}

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
    
    # Create formatters with enhanced visual style
    file_formatter = logging.Formatter(
        '%(asctime)s.%(msecs)03d │ %(levelname)-8s │ %(message)s',
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
    
    # Write header to log file
    _write_log_header(logger, timestamp)
    
    return logger


def _write_log_header(logger, timestamp):
    """写入日志文件头部信息"""
    header = f"""
{'='*100}
{'AlphaSolve 日志系统':^100}
{'='*100}
启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}
日志文件: {timestamp}.log
{'='*100}
"""
    # 直接写入文件，不通过标准格式化
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.stream.write(header + '\n')
            handler.flush()


def get_log_filename():
    """获取当前logger的日志文件名"""
    if hasattr(_thread_local, 'log_filename'):
        return _thread_local.log_filename
    return None


def _format_message(message: str, level: str, module: str = None) -> str:
    """
    格式化日志消息
    
    Args:
        message: 原始消息
        level: 日志级别
        module: 模块名称（可选）
    
    Returns:
        格式化后的消息
    """
    symbol = LEVEL_SYMBOLS.get(level.upper(), '📝')
    
    # 如果消息包含模块标识（如[solver]），则美化它
    if module:
        formatted_msg = f"{symbol} [{module}] {message}"
    elif message.strip().startswith('[') and ']' in message:
        # 自动检测模块标识
        formatted_msg = f"{symbol} {message}"
    else:
        formatted_msg = f"{symbol} {message}"
    
    return formatted_msg


def log_print(*args, sep=' ', end='\n', print_to_console: bool = True, level: str = 'INFO', module: str = None):
    """
    类似print的日志记录函数，同时记录到日志文件和控制台
    
    Args:
        *args: 要打印的内容
        sep: 分隔符
        end: 结束符
        print_to_console: 是否输出到控制台
        level: 日志级别 ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL', 'SUCCESS', 'METRIC')
        module: 模块名称，用于标识日志来源
    
    注意：当end=""时（流式输出），只打印到控制台不记录到日志，避免日志文件混乱
    """
    logger = get_logger(print_to_console=False)  # logger不输出到控制台
    
    # Join all arguments with separator
    message = sep.join(str(arg) for arg in args)
    
    # 打印到控制台（如果需要）
    if print_to_console:
        print(message, end=end, flush=True)
    
    # 记录到日志文件（只有end=='\n'时才记录，避免流式输出的碎片）
    if end == '\n' and message.strip():
        level = level.upper()
        formatted_msg = _format_message(message, level, module)
        
        # 特殊级别映射到标准级别
        if level in ('SUCCESS', 'METRIC'):
            logger.info(formatted_msg)
        elif level == 'DEBUG':
            logger.debug(formatted_msg)
        elif level == 'WARNING':
            logger.warning(formatted_msg)
        elif level == 'ERROR':
            logger.error(formatted_msg)
        elif level == 'CRITICAL':
            logger.critical(formatted_msg)
        else:  # INFO or default
            logger.info(formatted_msg)


def reset_logger():
    """重置logger实例，用于创建新的日志文件"""
    if hasattr(_thread_local, 'logger'):
        # Close all handlers
        for handler in _thread_local.logger.handlers[:]:
            handler.close()
            _thread_local.logger.removeHandler(handler)
        _thread_local.logger = None
        _thread_local.log_filename = None


def log_separator(style: str = 'line', width: int = 100, print_to_console: bool = True):
    """
    输出分隔线
    
    Args:
        style: 分隔线样式 ('line', 'double', 'dash', 'dot', 'section')
        width: 分隔线宽度
        print_to_console: 是否输出到控制台
    """
    styles = {
        'line': '─' * width,
        'double': '═' * width,
        'dash': '┈' * width,
        'dot': '·' * width,
        'section': '━' * width,
    }
    separator = styles.get(style, '─' * width)
    log_print(separator, print_to_console=print_to_console, level='INFO')


def log_section(title: str, width: int = 100, print_to_console: bool = True):
    """
    输出带标题的分节
    
    Args:
        title: 分节标题
        width: 宽度
        print_to_console: 是否输出到控制台
    """
    log_separator('section', width, print_to_console)
    centered_title = f"  {title}  "
    padding = (width - len(centered_title)) // 2
    formatted_title = '│' + ' ' * padding + centered_title + ' ' * (width - padding - len(centered_title) - 1) + '│'
    log_print(formatted_title, print_to_console=print_to_console, level='INFO')
    log_separator('section', width, print_to_console)


def log_box(message: str, width: int = 100, print_to_console: bool = True, level: str = 'INFO'):
    """
    输出带边框的消息
    
    Args:
        message: 消息内容
        width: 边框宽度
        print_to_console: 是否输出到控制台
        level: 日志级别
    """
    lines = message.split('\n')
    log_print('┌' + '─' * (width - 2) + '┐', print_to_console=print_to_console, level=level)
    for line in lines:
        padded_line = line + ' ' * (width - len(line) - 4)
        log_print(f'│ {padded_line} │', print_to_console=print_to_console, level=level)
    log_print('└' + '─' * (width - 2) + '┘', print_to_console=print_to_console, level=level)


def log_metric(metric_name: str, value, unit: str = '', print_to_console: bool = True):
    """
    记录指标信息（如耗时、长度等）
    
    Args:
        metric_name: 指标名称
        value: 指标值
        unit: 单位
        print_to_console: 是否输出到控制台
    """
    formatted_value = f"{value}{unit}" if unit else str(value)
    message = f"{metric_name}: {formatted_value}"
    log_print(message, print_to_console=print_to_console, level='METRIC')


def log_dict(data: dict, title: str = None, print_to_console: bool = True, level: str = 'INFO'):
    """
    美化输出字典数据
    
    Args:
        data: 字典数据
        title: 标题（可选）
        print_to_console: 是否输出到控制台
        level: 日志级别
    """
    if title:
        log_print(f"┌─ {title}", print_to_console=print_to_console, level=level)
    for key, value in data.items():
        log_print(f"│ {key}: {value}", print_to_console=print_to_console, level=level)
    if title:
        log_print("└─", print_to_console=print_to_console, level=level)


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


def success(*args, **kwargs):
    """记录SUCCESS级别日志（成功操作）"""
    log_print(*args, **kwargs, level='SUCCESS')


def metric(*args, **kwargs):
    """记录METRIC级别日志（指标数据）"""
    log_print(*args, **kwargs, level='METRIC')
