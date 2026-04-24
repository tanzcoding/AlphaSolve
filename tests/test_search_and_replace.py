import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alphasolve.utils.utils import search_and_replace


def test_search_and_replace_supports_marker_span_removal():
    target_text = (
        r"We prove that the solutions to the elastic wave equations and the ideal "
        r"compressible magnet-hydrodynamics equations (MHD) can be controlled for "
        r"short times for rough initial data ($H^{3+\frac{1}{4}+}$ in 3D, "
        r"$H^{2+\frac{7}{8}+}$ in 2D for the elastic wave equations, and "
        r"$H^{2+\frac{1}{4}}$ in 3D, $H^{1+\frac{7}{8}}$ in 2D for the ideal "
        r"compressible MHD)."
    )
    operation = r"""<<<<<<< SEARCH
        $H^{3+\frac{1}{4}+}$...wave equations, and
        =======
        >>>>>>> REPLACE"""

    result = search_and_replace(target_text, operation)

    assert r"$H^{3+\frac{1}{4}+}$" not in result
    assert "ideal compressible magnet-hydrodynamics" in result
    assert r"$H^{2+\frac{1}{4}}$" in result
