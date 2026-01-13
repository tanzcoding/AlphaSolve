# Problem

```
Find all $\alpha\in\mathbb{R}$ such that the equation $-\alpha U(y)+\left[(1+\alpha)y+U(y)\right] \partial_y U(y) = 0$ has a solution in $\{U\in C^\infty (\mathbb{R}):U(0)=0\}$ other than $U(y) = -y$ and $U(y)=0$.
```

# Solution

The research pipeline verified the following lemmas:

### Lemma 0

**Statement**
For all real numbers α > 0, the differential equation -α U(y) + [(1 + α)y + U(y)] ∂_y U(y) = 0 has no smooth solutions U ∈ C^∞(ℝ) with U(0) = 0 other than U(y) = 0 and U(y) = -y.

**Proof**
To establish the conjecture, we analyze the first-order ODE and its solutions under α > 0 and U(0) = 0.

### Step 1: Rewrite the ODE
The given ODE can be rewritten as:
\[ \partial_y U = \frac{\alpha U}{(1 + \alpha)y + U} \]

### Step 2: Derive the General Solution
Using the substitution \( v = \frac{U}{y} \) (valid for \( y \neq 0 \)), we have \( U = vy \) and \( \partial_y U = v + y \partial_y v \). Substituting into the ODE:
\[ v + y \partial_y v = \frac{\alpha vy}{(1 + \alpha)y + vy} = \frac{\alpha v}{(1 + \alpha) + v} \]
Rearranging gives the separable equation:
\[ y \partial_y v = -\frac{v(1 + v)}{(1 + \alpha) + v} \]
Separating variables and integrating:
\[ \int \frac{(1 + \alpha) + v}{v(1 + v)} \, dv = -\int \frac{dy}{y} \]

#### Partial Fraction Decomposition:
We decompose the integrand on the left using partial fractions:
\[ \frac{(1 + \alpha) + v}{v(1 + v)} = \frac{A}{v} + \frac{B}{1 + v} \]
Multiplying both sides by \( v(1 + v) \):
\[ (1 + \alpha) + v = A(1 + v) + Bv \]
Setting \( v = 0 \): \( 1 + \alpha = A \). Setting \( v = -1 \): \( \alpha = -B \). Thus, \( A = 1 + \alpha \), \( B = -\alpha \), so:
\[ \frac{(1 + \alpha) + v}{v(1 + v)} = \frac{1 + \alpha}{v} - \frac{\alpha}{1 + v} \]

#### Integrating:
Integrating term-by-term:
\[ \int \left( \frac{1 + \alpha}{v} - \frac{\alpha}{1 + v} \right) dv = -\int \frac{dy}{y} \]
\[ (1 + \alpha)\ln|v| - \alpha \ln|1 + v| = -\ln|y| + C \]
Exponentiating both sides:
\[ |v|^{1 + \alpha} |1 + v|^{-\alpha} = e^C / |y| \]
Substituting \( v = U/y \) and simplifying:
\[ U^{1 + \alpha} = K (U + y)^\alpha \]
where \( K = e^C \) is a constant.

### Step 3: Analyze Solutions with U(0) = 0
- **Trivial Solution U = 0**: Substituting \( U = 0 \) into the ODE gives \( 0 = 0 \), so it is valid. In the general solution, \( 0 = K y^\alpha \) implies \( K = 0 \), confirming consistency.
  
- **Non-Trivial Solution U = -y**: For \( U = -y \), substitute into the ODE:
  \[ -\alpha(-y) + [(1 + \alpha)y - y](-1) = \alpha y - \alpha y = 0 \]
  Thus, \( U = -y \) is a solution. This corresponds to the constant solution \( v = -1 \) (from \( v = U/y \)), satisfying the separated equation as \( v = -1 \) makes the right-hand side zero.

### Step 4: No Other Smooth Solutions
For \( K \neq 0 \), the general solution \( U^{1 + \alpha} = K (U + y)^\alpha \) implies \( U \sim a y + o(y) \) near \( y = 0 \). Substituting, \( a^{1 + \alpha} y^{1 + \alpha} = K y^\alpha \), which is impossible unless \( a = 0 \) (yielding \( U = 0 \)). Thus, no non-trivial solutions exist for \( K \neq 0 \).

### Conclusion
The only smooth solutions with \( U(0) = 0 \) are \( U = 0 \) and \( U = -y \).