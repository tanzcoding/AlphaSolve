#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
详细调试 Wolfram 会话
"""

import sys
import os
import time
import subprocess

# 检查内核文件是否存在
kernel_path = os.environ.get("WOLFRAM_KERNEL", "")
print(f"环境变量 WOLFRAM_KERNEL = {kernel_path}")

if not kernel_path:
    print("错误: 环境变量 WOLFRAM_KERNEL 未设置")
    sys.exit(1)

if not os.path.exists(kernel_path):
    print(f"错误: 内核文件不存在: {kernel_path}")
    sys.exit(1)

print(f"[OK] 内核文件存在: {kernel_path}")

# 尝试直接运行内核看看是否能启动
print("\n" + "=" * 60)
print("尝试直接运行 Wolfram 内核...")
print("=" * 60)

try:
    # 测试内核是否可以启动并输出版本信息
    result = subprocess.run(
        [kernel_path, "-version"],
        capture_output=True,
        text=True,
        timeout=30
    )
    print(f"返回码: {result.returncode}")
    if result.stdout:
        print(f"标准输出:\n{result.stdout}")
    if result.stderr:
        print(f"标准错误:\n{result.stderr}")
    
    if result.returncode == 0:
        print("[OK] Wolfram 内核可以正常启动")
    else:
        print("[FAIL] Wolfram 内核启动失败")
        
except subprocess.TimeoutExpired:
    print("[FAIL] 启动超时")
except Exception as e:
    print(f"[FAIL] 发生错误: {e}")

# 尝试用 wolframclient 启动
print("\n" + "=" * 60)
print("尝试用 wolframclient 启动会话...")
print("=" * 60)

try:
    from wolframclient.evaluation import WolframLanguageSession
    from wolframclient.language import wlexpr
    
    print("创建会话对象...")
    session = WolframLanguageSession(kernel_path)
    
    print("等待会话启动...")
    time.sleep(5)  # 给更多时间启动
    
    print("尝试计算...")
    try:
        result = session.evaluate(wlexpr("1 + 1"))
        print(f"结果: {result}")
        print("[OK] 计算成功!")
    except Exception as e:
        print(f"[FAIL] 计算失败: {e}")
        import traceback
        traceback.print_exc()
    
    print("关闭会话...")
    session.terminate()
    
except Exception as e:
    print(f"[FAIL] 会话创建失败: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 60)
print("调试完成")
