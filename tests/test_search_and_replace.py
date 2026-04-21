#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Test script for apply_unified_diff function"""

import sys
import os

# Add project root to sys.path to enable imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from utils.utils import search_and_replace
# Target text
target_text = r"""We prove that the solutions to the elastic wave equations and the ideal compressible magnet-hydrodynamics equations (MHD) can be controlled for short times for rough initial data ($H^{3+\frac{1}{4}+}$ in 3D, $H^{2+\frac{7}{8}+}$ in 2D for the elastic wave equations, and $H^{2+\frac{1}{4}}$ in 3D, $H^{1+\frac{7}{8}}$ in 2D for the ideal compressible MHD)."""

# Diff string
operation = r"""<<<<<<< SEARCH
        $H^{3+\frac{1}{4}+}$...wave equations, and 
        =======
        >>>>>>> REPLACE"""

def main():
    print("=" * 80)
    print("Testing search_and_replace function")
    print("=" * 80)
    print()

    print("Original text length:", len(target_text))
    print("operation string length:", len(operation))
    print()

    try:
        result = search_and_replace(target_text, operation)
        print("[OK] Operation applied successfully!")
        print(result)
        print("Result length:", len(result))
        print()

        return 0
    except Exception as e:
        print("[ERROR] Failed to apply operation:")
        print(e)
        return 1

if __name__ == "__main__":
    sys.exit(main())
