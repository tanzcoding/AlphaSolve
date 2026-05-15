#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wolfram Session 测试脚本
用于检查 wolframclient 是否能正常启动 Wolfram 会话并执行基本计算
"""

import sys
import os
import traceback
import io

# 设置标准输出为 UTF-8 编码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 常见的 Wolfram 内核路径（Windows）
COMMON_WOLFRAM_PATHS = [
    # Wolfram Engine
    r"C:\Program Files\Wolfram Research\Wolfram Engine\14.0\WolframKernel.exe",
    r"C:\Program Files\Wolfram Research\Wolfram Engine\13.3\WolframKernel.exe",
    r"C:\Program Files\Wolfram Research\Wolfram Engine\13.2\WolframKernel.exe",
    r"C:\Program Files\Wolfram Research\Wolfram Engine\13.1\WolframKernel.exe",
    r"C:\Program Files\Wolfram Research\Wolfram Engine\13.0\WolframKernel.exe",
    # Mathematica
    r"C:\Program Files\Wolfram Research\Mathematica\14.0\WolframKernel.exe",
    r"C:\Program Files\Wolfram Research\Mathematica\13.3\WolframKernel.exe",
    r"C:\Program Files\Wolfram Research\Mathematica\13.2\WolframKernel.exe",
    r"C:\Program Files\Wolfram Research\Mathematica\13.1\WolframKernel.exe",
    r"C:\Program Files\Wolfram Research\Mathematica\13.0\WolframKernel.exe",
]

def find_wolfram_kernel():
    """尝试查找 Wolfram 内核"""
    print("\n" + "=" * 60)
    print("尝试查找 Wolfram 内核...")
    print("=" * 60)
    
    # 检查环境变量
    env_kernel = os.environ.get("WOLFRAM_KERNEL")
    if env_kernel:
        print(f"[INFO] 环境变量 WOLFRAM_KERNEL = {env_kernel}")
        if os.path.exists(env_kernel):
            print(f"[OK] 环境变量指定的内核存在")
            return env_kernel
        else:
            print(f"[FAIL] 环境变量指定的内核不存在")
    
    # 检查常见路径
    print("\n检查常见安装路径:")
    for path in COMMON_WOLFRAM_PATHS:
        if os.path.exists(path):
            print(f"[OK] 找到内核: {path}")
            return path
        else:
            print(f"[SKIP] 不存在: {path}")
    
    print("\n[FAIL] 未找到 Wolfram 内核")
    return None

def test_wolfram_import():
    """测试 wolframclient 模块导入"""
    print("=" * 60)
    print("测试 1: 导入 wolframclient 模块")
    print("=" * 60)
    try:
        from wolframclient.language import wlexpr
        from wolframclient.evaluation import WolframLanguageSession
        print("[OK] 成功导入 wolframclient 模块")
        return True, (wlexpr, WolframLanguageSession)
    except ImportError as e:
        print(f"[FAIL] 导入失败: {e}")
        print("\n提示: 请确保已安装 wolframclient: pip install wolframclient")
        return False, None
    except Exception as e:
        print(f"[FAIL] 发生未知错误: {e}")
        traceback.print_exc()
        return False, None

def test_wolfram_session(WolframLanguageSession, wlexpr, kernel_path=None):
    """测试 Wolfram 会话启动和基本计算"""
    print("\n" + "=" * 60)
    print("测试 2: 启动 Wolfram 会话并执行计算")
    print("=" * 60)
    
    session = None
    try:
        print("正在启动 Wolfram 会话...")
        if kernel_path:
            print(f"使用内核路径: {kernel_path}")
            session = WolframLanguageSession(kernel_path)
        else:
            session = WolframLanguageSession()
        print("[OK] 成功启动 Wolfram 会话")
        
        # 测试简单计算
        test_cases = [
            ("2 + 2", "2 + 2"),
            ("Sin[Pi/2]", "Sin[Pi/2]"),
            ("Integrate[x^2, x]", "Integrate[x^2, x]"),
        ]
        
        for desc, code in test_cases:
            print(f"\n测试: {desc}")
            try:
                result = session.evaluate(wlexpr(code))
                print(f"  代码: {code}")
                print(f"  结果: {result}")
                print("  [OK] 成功")
            except Exception as e:
                print(f"  [FAIL] 失败: {e}")
        
        return True
        
    except Exception as e:
        print(f"[FAIL] Wolfram 会话测试失败: {e}")
        traceback.print_exc()
        print("\n提示:")
        print("1. 请确保已安装 Wolfram Engine 或 Mathematica")
        print("2. 请确保 Wolfram Engine/Mathematica 已正确激活")
        print("3. 尝试设置环境变量 WOLFRAM_KERNEL 指向 WolframKernel.exe")
        print("4. 尝试在命令行运行 'wolfram' 或 'math' 命令检查")
        return False
    finally:
        if session:
            try:
                print("\n正在关闭 Wolfram 会话...")
                session.terminate()
                print("[OK] 会话已关闭")
            except:
                pass

def main():
    print("Wolfram Session 测试")
    print("=" * 60)
    
    # 测试 1: 导入模块
    success, modules = test_wolfram_import()
    if not success:
        print("\n" + "=" * 60)
        print("测试失败: 无法导入 wolframclient")
        print("=" * 60)
        return 1
    
    wlexpr, WolframLanguageSession = modules
    
    # 尝试查找内核
    kernel_path = find_wolfram_kernel()
    
    # 测试 2: 会话和计算（首先尝试默认方式，失败则尝试找到的路径）
    success = test_wolfram_session(WolframLanguageSession, wlexpr)
    if not success and kernel_path:
        print("\n" + "=" * 60)
        print("使用找到的内核路径重试...")
        print("=" * 60)
        success = test_wolfram_session(WolframLanguageSession, wlexpr, kernel_path)
    
    print("\n" + "=" * 60)
    if success:
        print("所有测试通过!")
    else:
        print("部分测试失败")
        if kernel_path:
            print(f"\n建议设置环境变量: set WOLFRAM_KERNEL={kernel_path}")
    print("=" * 60)
    
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())
