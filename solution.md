# Problem

```
Let $v$ be a compactly supported smooth vector field on $\mathbb{R}^n$. Suppose that all $n$ eigenvalues of the Jacobian matrix $\nabla v$ of $v$ vanish everywhere on $\mathbb{R}^n$. Prove that $v$ also vanishes everywhere.
```

# Solution

The research pipeline verified the following lemmas:

### Lemma 14

**Statement**
[Vanishing of the $L^{2}$ inner products of the components]
Let $v\colon\mathbb{R}^{n}\to\mathbb{R}^{n}$ be a compactly supported smooth vector field such that at every point $x\in\mathbb{R}^{n}$ all eigenvalues of the Jacobian matrix $\nabla v(x)$ are zero. Then for every pair of indices $i,j\in\{1,\dots ,n\}$
\[
\int_{\mathbb{R}^{n}} v_i(x)\,v_j(x)\,dx =0 .
\]

**Proof**
Since all eigenvalues of $\nabla v(x)$ are zero, the characteristic polynomial of $\nabla v(x)$ is $\lambda^{n}$. By the Cayley–Hamilton theorem, $(\nabla v(x))^{n}=0$. In particular, for any integer $k\ge 1$ the trace of $(\nabla v(x))^{k}$ vanishes; hence
\[
\operatorname{tr}\!\bigl(\nabla v(x)\bigr)=0,\qquad 
\operatorname{tr}\!\bigl((\nabla v(x))^{2}\bigr)=0\qquad\text{for all }x\in\mathbb{R}^{n}.
\tag{1}
\]
The first equality is $\operatorname{div}v(x)=0$.

Fix two indices $j,k\in\{1,\dots ,n\}$ and consider the integral
\[
I_{jk}:=\int_{\mathbb{R}^{n}}\operatorname{tr}\!\bigl((\nabla v(x))^{2}\bigr)\,x_j x_k\,dx .
\]
Because $\operatorname{tr}\!\bigl((\nabla v)^{2}\bigr)$ vanishes pointwise, $I_{jk}=0$.

Expand the trace:
\[
\operatorname{tr}\!\bigl((\nabla v)^{2}\bigr)=\sum_{a,b=1}^{n}(\partial_{a}v_{b})(\partial_{b}v_{a}).
\]
Thus
\[
0=I_{jk}= \sum_{a,b}\int_{\mathbb{R}^{n}}(\partial_{a}v_{b})(\partial_{b}v_{a})\,x_j x_k\,dx .
\tag{2}
\]

Integrate by parts with respect to the variable $x_{a}$; the boundary terms disappear because $v$ is compactly supported.  For each term we obtain
\[
\int_{\mathbb{R}^{n}}(\partial_{a}v_{b})(\partial_{b}v_{a})\,x_j x_k\,dx
 =-\int_{\mathbb{R}^{n}}v_{b}\;\partial_{a}\!\bigl((\partial_{b}v_{a})\,x_j x_k\bigr)\,dx .
\]
Now
\[
\partial_{a}\!\bigl((\partial_{b}v_{a})\,x_j x_k\bigr)
 =(\partial_{a}\partial_{b}v_{a})\,x_j x_k+(\partial_{b}v_{a})\,\partial_{a}(x_j x_k),
\]
and $\partial_{a}(x_j x_k)=\delta_{aj}x_k+\delta_{ak}x_j$.  Substituting this into (2) gives
\[
0=-\sum_{a,b}\int_{\mathbb{R}^{n}}v_{b}
   \Bigl[(\partial_{a}\partial_{b}v_{a})\,x_j x_k
        +(\partial_{b}v_{a})(\delta_{aj}x_k+\delta_{ak}x_j)\Bigr]dx .
\tag{3}
\]

The first sum in (3) can be rewritten using the commutativity of partial derivatives and the divergence‑free condition:
\[
\sum_{a,b}\int_{\mathbb{R}^{n}}v_{b}(\partial_{a}\partial_{b}v_{a})\,x_j x_k\,dx
 =\sum_{a,b}\int_{\mathbb{R}^{n}}v_{b}\partial_{b}(\partial_{a}v_{a})\,x_j x_k\,dx
 =\sum_{b}\int_{\mathbb{R}^{n}}v_{b}\partial_{b}(\operatorname{div}v)\,x_j x_k\,dx=0,
\]
because $\operatorname{div}v\equiv0$.  Consequently (3) reduces to
\[
0=-\sum_{a,b}\int_{\mathbb{R}^{n}}v_{b}(\partial_{b}v_{a})(\delta_{aj}x_k+\delta_{ak}x_j)\,dx .
\]
Performing the summation over $a$ yields
\[
0=-\sum_{b}\int_{\mathbb{R}^{n}}v_{b}(\partial_{b}v_{j})\,x_k\,dx
    -\sum_{b}\int_{\mathbb{R}^{n}}v_{b}(\partial_{b}v_{k})\,x_j\,dx .
\tag{4}
\]

Define for any indices $\ell,m$
\[
A_{\ell,m}:=\sum_{b}\int_{\mathbb{R}^{n}}v_{b}(\partial_{b}v_{\ell})\,x_m\,dx .
\]
Equation (4) is $A_{j,k}+A_{k,j}=0$.

To evaluate $A_{\ell,m}$, use the identity
\[
v_{b}(\partial_{b}v_{\ell})=\partial_{b}(v_{b}v_{\ell})-(\partial_{b}v_{b})v_{\ell}.
\]
Integrating against $x_m$ gives
\[
\int_{\mathbb{R}^{n}}v_{b}(\partial_{b}v_{\ell})x_m\,dx
 =\int_{\mathbb{R}^{n}}\partial_{b}(v_{b}v_{\ell})x_m\,dx
   -\int_{\mathbb{R}^{n}}(\partial_{b}v_{b})v_{\ell}x_m\,dx .
\]
The first term is integrated by parts:
\[
\int_{\mathbb{R}^{n}}\partial_{b}(v_{b}v_{\ell})x_m\,dx
 =-\int_{\mathbb{R}^{n}}v_{b}v_{\ell}\,\partial_{b}x_m\,dx
 =-\delta_{bm}\int_{\mathbb{R}^{n}}v_{b}v_{\ell}\,dx .
\]
Hence
\[
\int_{\mathbb{R}^{n}}v_{b}(\partial_{b}v_{\ell})x_m\,dx
 =-\delta_{bm}\int_{\mathbb{R}^{n}}v_{b}v_{\ell}\,dx
   -\int_{\mathbb{R}^{n}}(\partial_{b}v_{b})v_{\ell}x_m\,dx .
\]
Summation over $b$ yields
\[
A_{\ell,m}= -\sum_{b}\delta_{bm}\int_{\mathbb{R}^{n}}v_{b}v_{\ell}\,dx
            -\sum_{b}\int_{\mathbb{R}^{n}}(\partial_{b}v_{b})v_{\ell}x_m\,dx .
\]
The first sum is $-\int_{\mathbb{R}^{n}}v_{m}v_{\ell}\,dx$.  In the second sum the factor $v_{\ell}x_m$ does not depend on $b$, therefore
\[
\sum_{b}\int_{\mathbb{R}^{n}}(\partial_{b}v_{b})v_{\ell}x_m\,dx
 =\int_{\mathbb{R}^{n}}\Bigl(\sum_{b}\partial_{b}v_{b}\Bigr)v_{\ell}x_m\,dx
 =\int_{\mathbb{R}^{n}}(\operatorname{div}v)\,v_{\ell}x_m\,dx .
\]
Since $\operatorname{div}v\equiv0$, this integral vanishes.  Consequently
\[
A_{\ell,m}= -\int_{\mathbb{R}^{n}}v_{m}v_{\ell}\,dx .
\tag{5}
\]

Now insert (5) into the relation $A_{j,k}+A_{k,j}=0$:
\[
0 = -\int_{\mathbb{R}^{n}}v_{k}v_{j}\,dx -\int_{\mathbb{R}^{n}}v_{j}v_{k}\,dx
   = -2\int_{\mathbb{R}^{n}}v_{j}v_{k}\,dx .
\]
Thus
\[
\int_{\mathbb{R}^{n}}v_{j}(x)v_{k}(x)\,dx =0\qquad\text{for all }j,k.
\tag{6}
\]

In particular, taking $j=k$ gives $\int_{\mathbb{R}^{n}}|v_{j}(x)|^{2}dx=0$.  Since $v_{j}$ is continuous, this forces $v_{j}\equiv0$ on $\mathbb{R}^{n}$.  As $j$ is arbitrary, we conclude $v\equiv0$.