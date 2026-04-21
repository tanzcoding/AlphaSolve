#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最终测试：验证项目的 Wolfram 集成是否正常工作
"""

import sys
import os

# 添加项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

print("=" * 60)
print("最终测试: 项目 Wolfram 集成")
print("=" * 60)

# 测试 1: 使用项目的 _start_wolfram_session
print("\n[1/3] 测试项目的 _start_wolfram_session 函数...")
from llms.utils import _start_wolfram_session

session = _start_wolfram_session(print_to_console=True)
if session is None:
    print("[FAIL] 无法启动会话")
    sys.exit(1)
print("[OK] 会话启动成功")

# 测试 2: 使用 run_wolfram 函数
print("\n[2/3] 测试项目的 run_wolfram 函数...")
from llms.tools import run_wolfram

test_cases = [
    "2 + 2",
    "Sin[Pi/2]", 
    "Integrate[x^2, x]",
]

all_passed = True
for code in test_cases:
    output, error = run_wolfram(code, session)
    if error:
        print(f"[FAIL] {code}: {error}")
        all_passed = False
    else:
        print(f"[OK] {code} = {output}")

# 测试 3: 验证会话状态保持
print("\n[3/3] 测试会话状态保持...")
# 先定义一个变量
output, error = run_wolfram("x = 10", session)
if error:
    print(f"[FAIL] 定义变量失败: {error}")
else:
    # 再使用这个变量
    output, error = run_wolfram("x * 2", session)
    if error:
        print(f"[FAIL] 使用变量失败: {error}")
    elif output == "20":
        print(f"[OK] 会话状态保持正常: x * 2 = {output}")
    else:
        print(f"[FAIL] 结果不正确: {output}")

# 清理
print("\n清理...")
session.terminate()
print("[OK] 会话已关闭")

print("\n" + "=" * 60)
if all_passed:
    print("所有测试通过!  ✓")
    print("\nWolfram 集成已正常工作，可以在项目中使用了。")
else:
    print("部分测试失败")
print("=" * 60)
