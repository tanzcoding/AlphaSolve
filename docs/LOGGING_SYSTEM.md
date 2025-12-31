# 日志系统文档

## 概述

本项目已集成完整的日志系统，使用Python的`logging`模块实现。所有控制台输出（`print`语句）已被替换为日志记录调用，确保运行过程中的所有信息都被记录到日志文件中。

## 主要特性

### 1. 自动时间戳文件命名
- 日志文件存储在`logs/`目录下
- 文件名格式：`YYYYMMDD_HHMMSS_mmm.log`（精确到毫秒）
- 例如：`20251231_093001_219.log`
- 支持高并发场景（如benchmark.py同时启动100个进程）

### 2. 双重输出
- **控制台输出**：可选择是否输出到控制台
- **文件记录**：始终记录到日志文件
- 通过`print_to_console`参数控制

### 3. 多进程安全
- 每个进程/线程独立的日志文件
- 使用线程本地存储（Thread-local storage）
- 避免多进程写入冲突

### 4. 多级别日志
支持标准日志级别：
- `DEBUG`: 调试信息
- `INFO`: 一般信息（默认）
- `WARNING`: 警告信息
- `ERROR`: 错误信息
- `CRITICAL`: 严重错误

## 使用方法

### 基本用法

```python
from utils.logger import log_print

# 记录到日志文件并输出到控制台
log_print("这是一条日志消息", print_to_console=True)

# 仅记录到日志文件，不输出到控制台
log_print("仅记录不显示", print_to_console=False)

# 多参数
log_print("参数1:", value1, "参数2:", value2, print_to_console=True)
```

### 指定日志级别

```python
from utils.logger import log_print

log_print("普通信息", print_to_console=True, level='INFO')
log_print("警告信息", print_to_console=True, level='WARNING')
log_print("错误信息", print_to_console=True, level='ERROR')
```

### 便捷函数

```python
from utils.logger import info, warning, error, debug, critical

info("这是INFO级别日志", print_to_console=True)
warning("这是WARNING级别日志", print_to_console=True)
error("这是ERROR级别日志", print_to_console=True)
```

### 获取日志文件路径

```python
from utils.logger import get_log_filename

log_file = get_log_filename()
print(f"当前日志文件: {log_file}")
```

## 日志格式

### 文件格式
```
[2025-12-31 09:30:01.220] [INFO] [AlphaSolve_20251231_093001_219] 日志消息内容
```

- **时间戳**：精确到毫秒
- **日志级别**：INFO/WARNING/ERROR等
- **Logger名称**：包含时间戳的唯一标识
- **消息内容**：实际的日志信息

### 控制台格式
控制台输出格式更简洁，直接显示消息内容，不包含时间戳和日志级别前缀。

## 项目集成情况

已在以下文件中替换所有`print`语句：

### 核心模块
- `main.py` - 主程序入口
- `workflow.py` - AlphaSolve工作流
- `benchmark.py` - 基准测试（支持多进程）

### Agent模块
- `agents/solver.py` - 求解器Agent
- `agents/verifier.py` - 验证器Agent
- `agents/refiner.py` - 改进器Agent
- `agents/summarizer.py` - 总结器Agent
- `agents/utils.py` - Agent工具函数
- `agents/common_agent_base.py` - Agent基类

### LLM模块
- `llms/utils.py` - LLM客户端工具
- `llms/tools.py` - 工具函数（保留内部print用于代码执行）

## 多进程场景

在`benchmark.py`中使用多进程时：
- 每个worker进程创建独立的日志文件
- 日志文件名包含毫秒级时间戳，确保唯一性
- 主进程和worker进程的日志分别记录

示例：运行`benchmark.py -n 100`时，会生成约100个独立的日志文件。

## 注意事项

1. **日志目录**：首次运行会自动创建`logs/`目录
2. **文件清理**：日志文件不会自动清理，需要手动管理
3. **编码**：日志文件使用UTF-8编码
4. **性能**：日志记录是异步的，不会显著影响程序性能
5. **print_to_console参数**：
   - `True`: 同时输出到控制台和文件
   - `False`: 仅记录到文件

## 测试

运行测试脚本验证日志系统：

```bash
python test_logging.py
```

测试内容包括：
- 基本日志输出
- 控制台/文件输出控制
- 多参数支持
- 不同日志级别
- 日志文件验证

## Git配置

`.gitignore`已配置忽略`logs/`目录，避免提交大量日志文件到版本控制系统。

## 迁移说明

从旧的`print`语句迁移到新的日志系统：

### 旧代码
```python
print('[solver] solver quota exhausted ...')
print(f'using: {elapsed}s, length: {length}')
```

### 新代码
```python
from utils.logger import log_print

log_print('[solver] solver quota exhausted ...', print_to_console=True)
log_print(f'using: {elapsed}s, length: {length}', print_to_console=True)
```

### 条件输出
```python
# 旧代码
if self.print_to_console:
    print('[solver] message')

# 新代码
if self.print_to_console:
    log_print('[solver] message', print_to_console=self.print_to_console)
```

## 技术实现

### 核心模块：`utils/logger.py`

- **get_logger()**: 获取/创建logger实例
- **log_print()**: 类似print的日志记录函数
- **reset_logger()**: 重置logger（用于创建新日志文件）
- 使用线程本地存储（ThreadLocal）确保每个线程独立的logger实例

### 日志文件命名
```python
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
log_filename = f"logs/{timestamp}.log"
```

毫秒级精度确保高并发场景下的文件名唯一性。
