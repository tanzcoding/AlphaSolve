# Orchestrator Harness Benchmark Expected Behavior

This document records the expected behavior for running
`alphasolve --agent --profile orchestrator -p --debug` on four real research workspace
snapshots. These expectations are meant to evaluate whether the orchestrator
can correctly read the current research state, especially
`verified_propositions` and the knowledge base, and then recommend the right
next proposition.

These expectations are not final mathematical conclusions. They are gold
targets for the next research recommendation under the current workspace
snapshots. 

## Benchmark Workspaces

The benchmark snapshots are the canonical benchmark inputs. Evaluation must run
directly inside the four directories under this absolute root:

```text
D:\AlphaSolve数学问题\workspace review test
```

For scripts or logs that need an ASCII-safe spelling, the same root can be
written as:

```text
D:\AlphaSolve\u6570\u5b66\u95ee\u9898\workspace review test
```

Do not replace these inputs with copied, renamed, normalized, or reduced
fixtures when judging benchmark behavior. The absolute paths matter because the
agent is being tested on realistic workspace layout, file names, and accumulated
research artifacts.

The four benchmark workspace directories are:

- `2d elastic wave gwp toy model`
- `bmo optimization`
- `real material blowup 1`
- `real material blowup 2`

Equivalently, the benchmark cases are:

- `D:\AlphaSolve数学问题\workspace review test\2d elastic wave gwp toy model`
- `D:\AlphaSolve数学问题\workspace review test\bmo optimization`
- `D:\AlphaSolve数学问题\workspace review test\real material blowup 1`
- `D:\AlphaSolve数学问题\workspace review test\real material blowup 2`

Run each benchmark command from inside the corresponding workspace directory,
not from the repository root.

## Evaluation Prompt

Use a prompt of this form:

```text
This is a working directory of the alphasolve program. The alphasolve program
will try to solve problem.md. It should stop only when verified_propositions
contains a proposition whose statement is sufficient to answer the original
problem. Please carefully evaluate the current research progress of alphasolve,
mainly by reading verified propositions and the knowledge base. Then think about
what proposition should be attempted next. For now, do not use the SpawnWorker
tool.
```

When scoring a response, check whether it:

- identifies the real current progress in `verified_propositions` and the
  knowledge base;
- avoids burying stronger conclusions that appear in proof tails or KB details;
- avoids premature victory, such as claiming immediate GWP or completed blowup;
- proposes a verifiable, local, and obstacle-aware next proposition;
- cites or summarizes the decisive evidence instead of merely repeating an
  index.

## 1. `2d elastic wave gwp toy model`

### Suggested Benchmark Target

The correct next-step recommendation should formulate and audit an exact
derivative-level modified ghost-weight energy system for each `|alpha| <= N`.
It should include the `a^2` correction, nonnegative `D_alpha^+`, and an explicit
decomposition into core, tail, far, `partial_t a`, nonlinear, and commutator
remainders.

It should not claim immediate GWP. It must track derivative losses explicitly,
especially tail terms producing `D_{N+1}^+` and `E_{N+K+1}`, and `partial_t a`
terms requiring `G_{|alpha|+2}` or higher energies. It must state absorption
constants such as `C_tail A2` and require verified inequalities before
absorption. It must retain the known speed restrictions until a rigorous
rescaling or full-parameter argument covers all dependent estimates.

The final answer should distinguish verified components, unverified assembly
gaps, and the precise additional theorem needed to close the bootstrap.

## 2. `bmo optimization`

### Suggested Benchmark Target

The response should explicitly state the strongest certified lower bound for
the original objective:

```text
c + 1985/2
>= 1/(12 sqrt(3)) + 2/sqrt(12) * exp(-1 - 20 sqrt(3))
> 1/(12 sqrt(3)).
```

It should explain that the strict improvement comes from the tiny positive set
where the logarithmic candidate

```text
f_0(t) = -10 - (1 + log t)/sqrt(12)
```

is positive, so the `|f|` term contributes
`2 int_{f_0>0} f_0 = 2 sigma t_0`.

It should not present `1/(12 sqrt(3))` as the final value of the original
problem.

## 3. `real material blowup 1`

### Suggested Benchmark Target

The response should identify the existing `delta`-slack finite speed result as
a major blocker and recommend attacking exact exterior-state finite propagation
speed at speed `c1` next.

It should capture the core distinction: generic first-order hyperbolic finite
speed is not automatically enough; the desired result is an exact support
statement from the steady exterior state `u=0, F=I`, with no `delta` slack and no
dependence on larger interior nonlinear characteristic speeds.

It should also say that exact FPS would only remove the support/ODE-timescale
obstruction. Nonlinear H3/far-field positivity for the actual solution remains
necessary downstream, so the response should not claim blowup merely from exact
finite propagation speed.

## 4. `real material blowup 2`

### Suggested Benchmark Target

The response should notice that exact finite propagation speed is already
available in this snapshot and should treat it as infrastructure, not as the
remaining main problem.

The correct next-step recommendation should scan material classes, parameters,
and initial data for a regime in which the classical solution can be shown to
satisfy a John's-type differential inequality, preferably throughout its
lifespan or at least until the ODE blowup time:

```text
X''(t) >= X(t) + c * X(t)^2 / (e^t (t+R0)).
```

The scan should be broad enough to include materials/parameters and genuinely
3D initial data. It should not merely ask whether finite propagation speed holds
or whether a local sign condition is positive. It should ask which regimes can
make the full John inequality work for the actual nonlinear solution, including
H3-type positivity, remainder control, and timescale compatibility.

The response should treat the current neo-Hookean regime as a strong candidate,
but not necessarily the only candidate. It should also report any remaining gap
if the John inequality is only justified on an interval shorter than the ODE
blowup timescale.

## Summary Of Gold Expectations

| Workspace | Gold next-step behavior |
|---|---|
| `2d elastic wave gwp toy model` | Recommend exact derivative-level modified ghost-weight energy assembly; do not claim immediate GWP. |
| `bmo optimization` | Explicitly state the strict certified lower bound above `1/(12 sqrt(3))`; do not claim optimality without an upper bound. |
| `real material blowup 1` | Prioritize exact exterior-state finite propagation speed at speed `c1`; H3 remains downstream, but the current `delta` support slack is the blocker. |
| `real material blowup 2` | Given exact FPS, scan materials, parameters, and genuinely 3D data for a lifespan-wide unconditional John's-type ODE inequality, including H3, remainder, and timescale control. |
