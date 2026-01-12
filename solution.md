# Problem

```
Find all $\alpha\in\mathbb{R}$ such that the equation $-\alpha U(y)+\left[(1+\alpha)y+U(y)\right] \partial_y U(y) = 0$ has a solution in $\{U\in C^\infty (\mathbb{R}):U(0)=0\}$ other than $U(y) = -y$ and $U(y)=0$.
```

# Solution

The research pipeline verified the following lemmas:

### Lemma 1

**Statement**
There are no real numbers \(\alpha \in \mathbb{R}\) for which the equation \( -\alpha U(y) + \left[(1+\alpha)y + U(y)\right] \partial_y U(y) = 0 \) has a solution in \( \{U \in C^\infty(\mathbb{R}) : U(0) = 0\} \) other than \( U(y) = 0 \) and \( U(y) = -y \).

**Proof**
To prove the conjecture, we analyze the given first-order ODE:  
\[
-\alpha U + \left[(1+\alpha)y + U\right] U' = 0, \tag{1}
\]  
where \(U \in C^\infty(\mathbb{R})\) with \(U(0) = 0\). We aim to show the only solutions are \(U=0\) and \(U=-y\).


### **Case 1: \(\alpha = 0\)**  
For \(\alpha=0\), equation (1) simplifies to \((y + U)U' = 0\). This implies either \(U' = 0\) (constant solution) or \(U = -y\). Since \(U(0)=0\), the constant solution must be \(U=0\). Thus, solutions are \(U=0\) and \(U=-y\).


### **Case 2: \(\alpha \neq 0\)**  
For \(\alpha \neq 0\), rewrite (1) as \(\left[(1+\alpha)y + U\right] U' = \alpha U\).  

#### **Step 2.1: Linear Solution**  
Assume \(U = Ky\) (linear solution, since \(U(0)=0\)). Substituting \(U=Ky\) and \(U'=K\) into (1) gives:  
\[
\left[(1+\alpha)y + Ky\right]K = \alpha Ky \implies K(1+\alpha + K)y = \alpha Ky.
\]  
Canceling \(y\) (for \(y \neq 0\)) and solving for \(K\) gives \(K=-1\), so \(U=-y\) is a solution.


#### **Step 2.2: Non-Linear Solutions**  
To rule out non-linear solutions, use the substitution \(U = vy\) (since \(U(0)=0\), \(v\) is smooth with \(v(0) = U'(0)\)). Substituting \(U=vy\) and \(U' = v + yv'\) into (1):  
\[
\left[(1+\alpha)y + vy\right](v + yv') = \alpha vy.
\]  
Simplifying for \(y \neq 0\):  
\[
(1+\alpha + v)(v + yv') = \alpha v \implies yv' = \frac{\alpha v - v(1+\alpha + v)}{1+\alpha + v} = -\frac{v(1 + v)}{1+\alpha + v}.
\]  
This is separable:  
\[
\frac{1+\alpha + v}{v(1 + v)} \, dv = -\frac{1}{y} \, dy. \tag{3}
\]  


#### **Step 2.3: Integrating and Ruling Non-Linear Solutions**  
Integrate (3) with partial fractions:  
\[
\int \left( \frac{1+\alpha}{v} - \frac{\alpha}{1+v} \right) dv = -\int \frac{1}{y} \, dy.
\]  
Left side: \((1+\alpha)\ln|v| - \alpha\ln|1+v| = \ln\left( \frac{v^{1+\alpha}}{(1+v)^{-\alpha}} \right)\).  
Right side: \(-\ln|y| + C\).  

Equating and exponentiating:  
\[ \frac{v^{1+\alpha}}{(1+v)^{-\alpha}} = \frac{K}{y} \implies v^{1+\alpha}(1+v)^{-\alpha} = \frac{K}{y}, \tag{4} \]  
where \(K = e^C\) is a constant.  


#### **Step 2.4: Smoothness at \(y=0\) Implies \(K=0\)**  
Since \(U \in C^\infty(\mathbb{R})\), \(v(y)\) is smooth at \(y=0\), so \(v(y) \to m = v(0)\) as \(y \to 0\). The left-hand side of (4) tends to \(m^{1+\alpha}(1+m)^{-\alpha}\) (a constant), while the right-hand side \(\frac{K}{y}\) is unbounded near \(y=0\) unless \(K=0\). Thus, \(K=0\), so:  
\[ v^{1+\alpha}(1+v)^{-\alpha} = 0. \]  


#### **Step 2.5: Solving for \(v\)**  
For real \(v\), this implies \(v=0\) (trivial solution) or \(v=-1\) (non-trivial solution, since \(1+v=0\) makes the left side zero). Thus, \(U=0\) or \(U=-y\).  


### **Conclusion**  
For all \(\alpha \in \mathbb{R}\), the only smooth solutions with \(U(0)=0\) are \(U=0\) and \(U=-y\).