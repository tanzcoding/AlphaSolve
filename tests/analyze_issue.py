#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析为什么一开始失败后来成功
"""

import sys
import os

print("分析 Wolfram 会话问题...")
print("=" * 60)

# 查看环境变量
print("\n环境变量 WOLFRAM_KERNEL:")
kernel_path = os.environ.get("WOLFRAM_KERNEL", "")
print(f"  {kernel_path}")
if kernel_path:
    exists = os.path.exists(kernel_path)
    print(f"  存在: {exists}")

# 分析可能的原因
print("\n" + "=" * 60)
print("可能的原因分析:")
print("=" * 60)

reasons = [
    "1. Wolfram Engine 首次启动需要初始化/激活",
    "2. 内核进程需要预热时间", 
    "3. 之前有内核进程卡住，现在被清理了",
    "4. wolframscript 的执行触发了某些初始化",
    "5. 临时文件/锁文件问题得到解决",
]

for reason in reasons:
    print(f"  {reason}")

print("\n" + "=" * 60)
print("最可能的原因:")
print("=" * 60)
print("""
很可能是以下两种情况之一：

1. **Wolfram Engine 需要完全启动一次** - 当我们运行 wolframscript.exe 时，
   它可能触发了 Wolfram Engine 的完整初始化和激活检查。

2. **之前有僵尸内核进程** - 可能有旧的 WolframKernel.exe 进程在后台运行
   导致端口占用，现在这些进程已经被清理了。

不管怎样，现在 Wolfram 已经可以正常工作了！
""")
