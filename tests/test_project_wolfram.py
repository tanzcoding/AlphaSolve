#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试项目中的 Wolfram 会话启动函数
"""

import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llms.utils import _start_wolfram_session
from llms.tools import run_wolfram

print("测试项目中的 _start_wolfram_session 函数")
print("=" * 60)

# 检查环境变量
env_kernel = os.environ.get("WOLFRAM_KERNEL")
if env_kernel:
    print(f"环境变量 WOLFRAM_KERNEL = {env_kernel}")
else:
    print("环境变量 WOLFRAM_KERNEL 未设置")

print("\n尝试启动 Wolfram 会话...")
session = _start_wolfram_session(print_to_console=True)

if session is None:
    print("\n[FAIL] 无法启动 Wolfram 会话")
    print("\n可能的原因:")
    print("1. 未安装 Wolfram Engine 或 Mathematica")
    print("2. Wolfram Engine/Mathematica 未正确激活")
    print("3. 需要设置 WOLFRAM_KERNEL 环境变量指向 WolframKernel.exe")
    sys.exit(1)

print("\n[OK] Wolfram 会话启动成功!")

# 测试简单计算
print("\n测试 Wolfram 计算...")
try:
    from wolframclient.language import wlexpr
    result = session.evaluate(wlexpr("2 + 2"))
    print(f"2 + 2 = {result}")
    
    result = session.evaluate(wlexpr("Sin[Pi/2]"))
    print(f"Sin[Pi/2] = {result}")
    
    print("\n[OK] 所有计算测试通过!")
except Exception as e:
    print(f"[FAIL] 计算测试失败: {e}")

# 清理
print("\n关闭会话...")
try:
    session.terminate()
    print("[OK] 会话已关闭")
except:
    pass

print("\n" + "=" * 60)
print("测试完成!")
