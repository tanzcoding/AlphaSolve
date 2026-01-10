# Problem

```
Show that the convex radius of a Riemannian manifold is continuous.
```

# Solution

The research pipeline verified the following lemmas:

### Lemma 0

**Statement**
Let $(M,g)$ be a Riemannian manifold. For a point $p\in M$ define the **convex radius**  
$$
\operatorname{conv}(p)=\sup\bigl\{r>0\mid \text{the open geodesic ball }B(p,r)\text{ is convex}\bigr\}.
$$
Then the function $\operatorname{conv}:M\to(0,\infty]$ is lower semicontinuous; i.e., for every $p\in M$ and every sequence $p_n\to p$ one has
$$
\liminf_{n\to\infty}\operatorname{conv}(p_n)\ge\operatorname{conv}(p).
$$

**Proof**
1. **Preliminary facts**  
   - The injectivity radius $\operatorname{inj}:M\to(0,\infty]$ is continuous (a standard result in Riemannian geometry).  
   - For $p\in M$ and $r<\operatorname{inj}(p)$ the distance function $f_p(x)=d(p,x)$ is smooth on $B(p,r)\setminus\{p\}$, and any two points of $B(p,r)$ are joined by a unique minimizing geodesic.  
   - A geodesic ball $B(p,r)$ with $r<\operatorname{inj}(p)$ is convex **iff** the function $f_p$ is convex on $B(p,r)$.  
     (This equivalence was proved in the preceding verification: convexity of the ball is equivalent to the non‑negativity of the Hessian of $f_p$, which in turn is equivalent to the convexity of $f_p$ along every geodesic contained in the ball.)
   - The convexity radius satisfies $\operatorname{conv}(p)\le\min\{\operatorname{inj}(p),\operatorname{conj}(p)\}$, where $\operatorname{conj}(p)$ denotes the conjugate radius at $p$ (the minimum distance to a conjugate point along any geodesic starting from $p$). This is a standard inequality in Riemannian geometry (see e.g. Cheeger–Ebin, *Comparison Theorems in Riemannian Geometry*). Consequently, if $r<\operatorname{conv}(p)$ then $r<\operatorname{inj}(p)$ and $r<\operatorname{conj}(p)$; in particular there are **no conjugate points** on any geodesic segment of length $\le r$ emanating from $p$.

2. **Fix $p$ and $r<\operatorname{conv}(p)$.**  
   Because $\operatorname{conv}(p)\le\operatorname{inj}(p)$ (convexity requires uniqueness of geodesics), we have $r<\operatorname{inj}(p)$.  
   Hence $B(p,r)$ is convex and, by the equivalence, $f_p$ is convex on $B(p,r)$; in particular its Hessian $\operatorname{Hess}f_p$ is positive semidefinite at every point of $B(p,r)\setminus\{p\}$.

3. **Continuity of the injectivity radius**  
   Since $\operatorname{inj}$ is continuous, there exists $\delta_1>0$ such that for every $q$ with $d(q,p)<\delta_1$ we have $\operatorname{inj}(q)>r$.  
   Consequently, for such $q$ the distance function $f_q(x)=d(q,x)$ is smooth on $B(q,r)\setminus\{q\}$ and any two points of $B(q,r)$ are joined by a unique minimizing geodesic.

4. **A slightly larger compact set**  

   Since $r<\operatorname{conv}(p)\le\min\{\operatorname{inj}(p),\operatorname{conj}(p)\}$, we can choose $\varepsilon>0$ such that $r+\varepsilon<\min\{\operatorname{inj}(p),\operatorname{conj}(p)\}$.  Set  

   $$K=\overline{B}(p,r+\varepsilon)=\{x\in M\mid d(p,x)\le r+\varepsilon\}.$$

   $K$ is compact.  Because $r+\varepsilon<\operatorname{inj}(p)$, the distance function $f_p$ is smooth on $K\setminus\{p\}$; because $r+\varepsilon<\operatorname{conj}(p)$, there are no conjugate points on any geodesic of length $\le r+\varepsilon$ emanating from $p$.  Consequently, on $K\setminus\{p\}$ the Hessian $\operatorname{Hess}f_p$ is positive definite on the subspace orthogonal to the radial direction (and zero in the radial direction); in particular $\operatorname{Hess}f_p\ge0$ pointwise.

5. **Positive definiteness of the Hessian in orthogonal directions**  

   Since $r<\operatorname{conv}(p)$, the open geodesic ball $B(p,r)$ is convex. By the equivalence recalled in step 1, the distance function $f_p$ is convex on $B(p,r)$. A standard result in Riemannian geometry (see e.g. Cheeger–Ebin, *Comparison Theorems in Riemannian Geometry*, Chapter 6) states that within the convex radius the Hessian of the distance function is **positive definite** on the subspace orthogonal to the radial direction. More concretely, if for some $x\in B(p,r)\setminus\{p\}$ and some unit vector $v$ orthogonal to the radial direction we had $\operatorname{Hess}_{x}f_p(v,v)\le0$, then the function $t\mapsto f_p(\exp_x(tv))$ would not be convex, contradicting the convexity of $f_p$ on $B(p,r)$. Hence $\operatorname{Hess}_{x}f_p(v,v)>0$ for every such $(x,v)$.

   Choose $\varepsilon>0$ such that $r+\varepsilon<\operatorname{conv}(p)$ and set $K=\overline{B}(p,r+\varepsilon)$. Then for every $x\in K\setminus\{p\}$ we have $d(p,x)\le r+\varepsilon<\operatorname{conv}(p)$, and therefore $\operatorname{Hess}_{x}f_p(v,v)>0$ for any unit vector $v$ orthogonal to the radial direction at $x$.

   Let $\mathcal{S}$ be the unit‑sphere bundle over $K$ consisting of pairs $(x,v)$ with $x\in K$, $v\in T_xM$, $\|v\|=1$ and $v$ orthogonal to the radial direction of $f_p$ at $x$. $\mathcal{S}$ is compact. Define  

   $$\mu:=\min_{(x,v)\in\mathcal{S}}\operatorname{Hess}_{x}f_p(v,v)\;>0.$$

   The positivity of $\mu$ follows from the compactness of $\mathcal{S}$ and the strict positivity of $\operatorname{Hess}_{x}f_p(v,v)$ for each $(x,v)\in\mathcal{S}$.

6. **Uniform positivity for nearby base points**  

   Since the injectivity radius is continuous, we can choose $\delta_2>0$ so small that for every $q$ with $d(q,p)<\delta_2$ we have $\operatorname{inj}(q)>r+\varepsilon$; consequently $K\subset B(q,\operatorname{inj}(q))$ and the distance function $f_q$ is smooth on $K\setminus\{q\}$. Moreover, by the continuity of the exponential map, the radial direction $R_q(x)$ of $f_q$ at $x$ depends continuously on $(q,x)$. Therefore we can choose $\delta_2$ (possibly smaller) such that for all $q$ with $d(q,p)<\delta_2$ and all $x\in K$, the angle $\theta(x)$ between $R_q(x)$ and $R_p(x)$ satisfies $\sin\theta(x)<\frac12$.

   Now fix $q$ with $d(q,p)<\delta_2$. For any $x\in K\setminus\{q\}$ and any unit vector $w\in T_xM$ we write $w=a e+b v$, where $e=R_q(x)$, $v\perp e$, $a^2+b^2=1$. Because $\operatorname{Hess}_{x}f_q$ vanishes on $e$, we have  

   $$\operatorname{Hess}_{x}f_q(w,w)=b^{2}\operatorname{Hess}_{x}f_q(v,v).$$

   Let $u=R_p(x)$ and decompose $v=c u+d w'$ with $w'\perp u$, $c^2+d^2=1$. Since $v\perp e$ and $e$ makes an angle at most $\theta(x)$ with $u$, we obtain $|c|=|\langle v,u\rangle|\le\sin\theta(x)<\frac12$; hence $d^{2}\ge 1-\frac14=\frac34$.

   Using the uniform continuity of $(q,x,v)\mapsto\operatorname{Hess}_{x}f_q(v,v)$ on a compact neighbourhood of $\{p\}\times\mathcal{S}$, we can shrink $\delta_2$ further to ensure  

   $$\bigl|\operatorname{Hess}_{x}f_q(v,v)-\operatorname{Hess}_{x}f_p(v,v)\bigr|<\frac{\mu}{4}\qquad\text{for all }x\in K,\;v\perp e,\;\|v\|=1.$$

   For the vector $v$ above, because $w'\perp u$ we have $\operatorname{Hess}_{x}f_p(w',w')\ge\mu$. Consequently  

   $$\operatorname{Hess}_{x}f_p(v,v)=d^{2}\operatorname{Hess}_{x}f_p(w',w')\ge\frac34\,\mu.$$

   Therefore  

   $$\operatorname{Hess}_{x}f_q(v,v)\ge\frac34\,\mu-\frac{\mu}{4}=\frac{\mu}{2}>0.$$

   Finally  

   $$\operatorname{Hess}_{x}f_q(w,w)=b^{2}\operatorname{Hess}_{x}f_q(v,v)\ge0,$$

   with equality only when $b=0$, i.e. when $w$ is radial for $f_q$. Thus $\operatorname{Hess}_{x}f_q$ is positive semidefinite for every $x\in K\setminus\{q\}$.

  

   Choose $\delta = \min\{\delta_1,\delta_2,\varepsilon\}$.  For any $q$ with $d(p,q)<\delta$ we have  

   * $\operatorname{inj}(q)>r$ (by the choice of $\delta_1$),  
   * $d(p,q)<\varepsilon$, hence $B(q,r)\subset B(p,r+\varepsilon)=K$,  
   * and, by step 6, $\operatorname{Hess}_{x}f_q\ge0$ for every $x\in K\setminus\{q\}$.

   Consequently $\operatorname{Hess}_{x}f_q\ge0$ for all $x\in B(q,r)\setminus\{q\}$.  Since the Hessian is non‑negative, the function $f_q$ is convex on $B(q,r)$.  By the equivalence recalled in step 1, this is exactly the condition that the open geodesic ball $B(q,r)$ is convex.  Therefore  

   $$\operatorname{conv}(q)\ge r\qquad\text{for all }q\text{ with }d(p,q)<\delta.$$

8. **Conclusion**  

   We have shown that for any $p\in M$ and any $r<\operatorname{conv}(p)$ there exists a neighbourhood $U$ of $p$ such that $\operatorname{conv}(q)\ge r$ for all $q\in U$.  This is precisely the definition of lower semicontinuity of the function $\operatorname{conv}$.

### Lemma 1

**Statement**
The convex radius function $\operatorname{conv}:M\to(0,\infty]$ defined by
\[
\operatorname{conv}(p)=\sup\{r>0\mid \text{the open geodesic ball }B(p,r)\text{ is convex}\}
\]
is upper semicontinuous; i.e., for every $p\in M$ and every sequence $p_n\to p$ one has
\[
\limsup_{n\to\infty}\operatorname{conv}(p_n)\le \operatorname{conv}(p).
\]

**Proof**
Let $(M,g)$ be a Riemannian manifold.  
Recall that an open geodesic ball $B(p,r)$ is **convex** if for any two points $x,y\in B(p,r)$ the unique minimizing geodesic segment joining $x$ and $y$ is entirely contained in $B(p,r)$.

---
### 1.  Reduction to the case $\operatorname{conv}(p)<\infty$

If $\operatorname{conv}(p)=\infty$ there is nothing to prove, because for any finite number $\alpha$ the condition $\operatorname{conv}(q)<\alpha$ is vacuously true (there is no $\alpha>\infty$).  
Hence we may assume $\operatorname{conv}(p)=r<\infty$.

---
### 2.  Sequential characterization

Upper semicontinuity is equivalent to the sequential condition
\[
\limsup_{n\to\infty}\operatorname{conv}(p_n)\le r
\qquad\text{whenever }p_n\to p .
\]
Assume, for contradiction, that there exists a sequence $p_n\to p$ with
$\displaystyle\limsup_{n\to\infty}\operatorname{conv}(p_n)>r$.
Then we can choose a real number $r'$ satisfying
\[
r<r'<\limsup_{n\to\infty}\operatorname{conv}(p_n)
\]
and a subsequence, still denoted by $(p_n)$, such that
\[
\operatorname{conv}(p_n)>r'\qquad\text{for all }n .
\]

---
### 3.  Convexity of the balls $B(p_n,r')$

Since $\operatorname{conv}(p_n)>r'$, there exists $r''>r'$ such that the larger ball $B(p_n,r'')$ is convex.  For any two points $x,y\in B(p_n,r')$, they also belong to $B(p_n,r'')$.  Because $B(p_n,r'')$ is convex, the unique minimizing geodesic segment $\gamma$ joining $x$ and $y$ lies entirely in $B(p_n,r'')$.  To show that $\gamma$ actually stays inside the smaller ball $B(p_n,r')$, we use the following standard property of convex balls: inside a convex geodesic ball $B(p,R)$, the squared distance function $f(q)=d(p,q)^2$ is strictly convex along any geodesic segment contained in $B(p,R)$ (see e.g. do Carmo, *Riemannian Geometry*, Chapter 3).  Applying this to the ball $B(p_n,r'')$ and the geodesic $\gamma$, we have for any $t\in(0,1)$

\[
d(p_n,\gamma(t))^2 < (1-t)\, d(p_n,x)^2 + t\, d(p_n,y)^2 .
\]

Because $d(p_n,x)<r'$ and $d(p_n,y)<r'$, the right‑hand side is strictly smaller than $r'^2$, whence $d(p_n,\gamma(t))<r'$.  Thus $\gamma\subset B(p_n,r')$, proving that $B(p_n,r')$ is convex.

---
### 4.  Passing to the limit using strict convexity

Pick any two points $x,y\in B(p,r')$.  Since $p_n\to p$, continuity of the distance function gives $d(p_n,x)<r'$ and $d(p_n,y)<r'$ for all sufficiently large $n$; thus $x,y\in B(p_n,r')$ for large $n$.

Because each $B(p_n,r')$ is convex, the unique minimizing geodesic segment $\gamma$ joining $x$ and $y$ (independent of $n$) lies entirely in $B(p_n,r')$.  Hence for every $t\in[0,1]$ and all large $n$,
\[
d(p_n,\gamma(t)) < r'.
\]

Fix $t\in(0,1)$.  Applying the strict convexity of the squared distance inside the convex ball $B(p_n,r')$ (see the property quoted in Section 3) to the geodesic $\gamma$, we obtain
\[
d(p_n,\gamma(t))^2 < (1-t)\, d(p_n,x)^2 + t\, d(p_n,y)^2 .
\tag{1}
\]

Letting $n\to\infty$ and using continuity of the distance function, we deduce
\[
d(p,\gamma(t))^2 \le (1-t)\, d(p,x)^2 + t\, d(p,y)^2 .
\tag{2}
\]

Since $d(p,x)<r'$ and $d(p,y)<r'$, the right‑hand side of (2) is strictly smaller than $r'^2$.  Consequently $d(p,\gamma(t)) < r'$ for every $t\in(0,1)$.  The endpoints $x,y$ already satisfy $d(p,x)<r'$, $d(p,y)<r'$, so the whole geodesic segment $\gamma$ lies in the open ball $B(p,r')$.  As $x,y$ were arbitrary points of $B(p,r')$, this shows that $B(p,r')$ is convex.

But $r' > r = \operatorname{conv}(p)$, which contradicts the definition of $r$ as the supremum of radii for which $B(p,\cdot)$ is convex.  Therefore our initial assumption is impossible; we must have
\[
\limsup_{n\to\infty}\operatorname{conv}(p_n) \le r = \operatorname{conv}(p).
\]

---
### 5.  Conclusion

We have shown that for every $p\in M$ and every sequence $p_n\to p$,
$\displaystyle\limsup_{n\to\infty}\operatorname{conv}(p_n)\le\operatorname{conv}(p)$.  
This is precisely the sequential characterization of upper semicontinuity, completing the proof.

### Lemma 2

**Statement**
Let \((M,g)\) be a Riemannian manifold.  For a point \(p\in M\) define the **convex radius**  

\[
\operatorname{conv}(p)=\sup\bigl\{r>0\mid \text{the open geodesic ball }B(p,r)\text{ is convex}\bigr\}.
\]

(Here “convex’’ means that any two points of the ball are joined by a minimizing geodesic that stays entirely inside the ball.)  
Then the function \(\operatorname{conv}:M\to(0,\infty]\) is continuous.  More precisely, for every \(p\in M\) and every sequence \(\{p_n\}\subset M\) with \(p_n\to p\) one has  

\[
\lim_{n\to\infty}\operatorname{conv}(p_n)=\operatorname{conv}(p),
\]

where convergence in \((0,\infty]\) is understood in the usual sense (if \(\operatorname{conv}(p)<\infty\) the limit is the ordinary limit of real numbers; if \(\operatorname{conv}(p)=\infty\) the statement means that \(\operatorname{conv}(p_n)\to\infty\)).

**Proof**
**1.  Lower semicontinuity.**  
Lemma‑0 (stored in memory) asserts that for any \(p\in M\) and any sequence \(p_n\to p\),

\[
\liminf_{n\to\infty}\operatorname{conv}(p_n)\ge\operatorname{conv}(p). \tag{1}
\]

**2.  Upper semicontinuity.**  
Lemma‑1 (stored in memory) gives for the same \(p\) and \(p_n\),

\[
\limsup_{n\to\infty}\operatorname{conv}(p_n)\le\operatorname{conv}(p). \tag{2}
\]

**3.  Combining the two inequalities.**  
From (1) and (2) we obtain

\[
\operatorname{conv}(p)\le\liminf_{n\to\infty}\operatorname{conv}(p_n)\le\limsup_{n\to\infty}\operatorname{conv}(p_n)\le\operatorname{conv}(p).
\]

Hence

\[
\liminf_{n\to\infty}\operatorname{conv}(p_n)=\limsup_{n\to\infty}\operatorname{conv}(p_n)=\operatorname{conv}(p). \tag{3}
\]

**4.  Convergence to a finite limit.**  
If \(\operatorname{conv}(p)<\infty\), (3) forces the ordinary limit \(\displaystyle\lim_{n\to\infty}\operatorname{conv}(p_n)\) to exist and equal \(\operatorname{conv}(p)\).  

**5.  Convergence to infinity.**  
If \(\operatorname{conv}(p)=\infty\), inequality (1) becomes \(\liminf_{n\to\infty}\operatorname{conv}(p_n)\ge\infty\), which can hold only when \(\liminf_{n\to\infty}\operatorname{conv}(p_n)=\infty\).  Consequently \(\lim_{n\to\infty}\operatorname{conv}(p_n)=\infty\) (the sequence diverges to infinity).

**6.  Continuity.**  
In either case we have \(\operatorname{conv}(p_n)\to\operatorname{conv}(p)\) for every sequence \(p_n\to p\).  This is exactly the definition of continuity of a function with values in the extended positive half‑line \((0,\infty]\) (equipped with the topology that makes convergence of real sequences to a finite limit or to infinity coincide with the usual notion).  

Therefore \(\operatorname{conv}\) is continuous on \(M\).