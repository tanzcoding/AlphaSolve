#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直接测试 Wolfram 内核的原始输出
"""

import subprocess
import sys
import time

kernel_path = r"D:\software\Wolfram Engine\WolframKernel.exe"

print("测试 1: 检查文件是否存在")
import os
if not os.path.exists(kernel_path):
    print(f"[FAIL] 文件不存在: {kernel_path}")
    sys.exit(1)
print(f"[OK] 文件存在: {kernel_path}")

print("\n测试 2: 使用 subprocess.Popen 启动内核并捕获所有输出")
print("-" * 60)

try:
    # 启动内核进程
    proc = subprocess.Popen(
        [kernel_path, "-noinit"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=0
    )
    
    # 发送命令
    commands = [
        "2 + 2\n",
        "Sin[Pi/2]\n", 
        "Quit[]\n"
    ]
    
    print("发送命令...")
    for cmd in commands:
        proc.stdin.write(cmd)
        proc.stdin.flush()
        time.sleep(1)
    
    # 等待进程结束
    print("等待进程结束...")
    stdout, stderr = proc.communicate(timeout=30)
    
    print(f"\n返回码: {proc.returncode}")
    
    if stdout:
        print(f"\n标准输出 ({len(stdout)} 字节):")
        print("-" * 60)
        print(repr(stdout))
        print("-" * 60)
    else:
        print("\n标准输出: (空)")
        
    if stderr:
        print(f"\n标准错误 ({len(stderr)} 字节):")
        print("-" * 60)
        print(repr(stderr))
        print("-" * 60)
    else:
        print("\n标准错误: (空)")
        
except subprocess.TimeoutExpired:
    print("\n[FAIL] 超时")
    proc.kill()
except Exception as e:
    print(f"\n[FAIL] 错误: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 60)
print("测试完成")
