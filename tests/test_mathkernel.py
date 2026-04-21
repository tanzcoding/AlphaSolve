#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试不同的 Wolfram 内核可执行文件
"""

import sys
import os

# 添加项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wolframclient.evaluation import WolframLanguageSession
from wolframclient.language import wlexpr

# 尝试不同的内核路径
kernel_paths = [
    r"D:\software\Wolfram Engine\WolframKernel.exe",
    r"D:\software\Wolfram Engine\MathKernel.exe",
]

for kernel_path in kernel_paths:
    print("\n" + "=" * 60)
    print(f"测试内核: {kernel_path}")
    print("=" * 60)
    
    if not os.path.exists(kernel_path):
        print("[FAIL] 文件不存在")
        continue
        
    try:
        print("正在创建会话...")
        session = WolframLanguageSession(kernel_path)
        
        print("正在计算 2+2 ...")
        result = session.evaluate(wlexpr("2 + 2"))
        print(f"结果: {result}")
        
        if result == 4:
            print("[OK] 成功!")
            
            print("\n正在测试更多计算...")
            result2 = session.evaluate(wlexpr("Sin[Pi/2]"))
            print(f"Sin[Pi/2] = {result2}")
            
            result3 = session.evaluate(wlexpr("Integrate[x^2, x]"))
            print(f"Integrate[x^2, x] = {result3}")
            
            print("\n[OK] 所有测试通过!")
            session.terminate()
            
            print(f"\n建议: 设置环境变量 WOLFRAM_KERNEL={kernel_path}")
            sys.exit(0)
        else:
            print(f"[FAIL] 结果不正确: {result}")
            
        session.terminate()
        
    except Exception as e:
        print(f"[FAIL] 错误: {e}")
        import traceback
        traceback.print_exc()

print("\n" + "=" * 60)
print("所有内核测试失败")
print("=" * 60)
