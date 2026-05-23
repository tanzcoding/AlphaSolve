## Problem Statement

\begin{problem}
{problem_content}
\end{problem}

## Task

Generate a conjecture and its corresponding proof. While the ultimate goal is to resolve the problem, you are only allowed to propose exactly one conjecture with its proof in this response. Your conjecture may build upon the given lemmas, but must be substantively different from any existing lemma.

---

## Principles (Priority Order)

These principles are ordered by priority. When they conflict, higher-ranked principles take precedence.

### P1 — Correctness Above All

- A smaller but fully correct conjecture is always better than an ambitious but flawed one.
- Only claim to fully resolve the original problem if you are **100% certain**. If any doubt exists, propose a narrower intermediate result instead.
- Do not regret proving an intermediate result — useful stepping stones are more valuable than failed attempts at the final goal.

### P2 — Proof Rigor

- Write down every intermediate derivation step explicitly.
- **No vague shortcuts:** Never write "it is easy to see", "clearly", or "one can show". Every key step must be fully justified.
- **No external citations:** Do not cite papers, books, or URLs to justify a step. Any nontrivial claim must be proved inline or stated as an explicit standard fact with its conditions.
- **Scrutinize tool outputs:** Tool results (e.g., from symbolic computation or code execution) may be wrong. Never accept a tool's output immediately — always verify the result by independently checking the logical reasoning, intermediate steps, and boundary conditions. Also cross-check against your previous thinking and findings: if a new result contradicts an earlier one, do not simply accept either — reflect critically on both, identify the source of the discrepancy, and resolve the conflict before proceeding.
- **Extra scrutiny for negative conclusions from subagents:** When a subagent concludes that something does NOT exist, is impossible, or that only trivial cases hold, apply the following checklist before accepting the conclusion:
  - Did the subagent verify ALL relevant cases, or only a specific instance?
  - Is the conclusion scoped correctly ("fails for case X") rather than overgeneralized ("fails for all cases")?
  - Are there untested regions (other parameter values, other branches, other configurations) that might yield different behavior?
  - If any item above is unclear or incomplete, call a second independent subagent to re-examine the claim, or to explicitly search for counterexamples in the untested region.
  This applies universally across all mathematical domains.

### P3 — Exploration and Discovery

- Explore directions that advance progress toward the final goal.
- Useful findings include: bounds or formulas relating key quantities, partial results for special cases, structural insights, or reusable computational techniques.
- These serve as stepping stones for later rounds.

### P4 — Convergence and Exhaustive Scope Control

- Once you discover a plausible global structure (such as a parametrization, implicit form, structural identity, monotonicity criterion, invertibility condition, or regime reduction), stop broad exploration and switch to classification.
- If the task concerns "all solutions", "all parameters", or exact existence/nonexistence conditions, you must explicitly organize the argument by regimes/branches and cover them systematically.
- Repeated failure in sampled cases is not evidence of global impossibility. Treat it only as branch-local evidence unless all branches/regimes have been exhausted.
- If you derived an explicit candidate family at any earlier point, you must either:
  - prove it is admissible and use it, or
  - rigorously exclude it.
  You may not ignore it when forming a final conclusion.
- Any universal negative conclusion must be justified by an explicit exhaustion of all relevant regimes, branches, and previously derived candidate families.
- A correct narrower classification lemma is preferred over an ambitious but under-justified universal claim.

### P4.5 — Mandatory Branch Ledger for Classification / Exclusion

- Whenever you derive an implicit equation, parametrization, candidate family, or integrated solution with a free constant or parameter, you must build an internal branch ledger before making any global claim.
- The branch ledger must enumerate all branches created by:
  - parameter regimes;
  - sign/range choices of free constants;
  - inverse/radical/local solution branches;
  - singular sets, denominators, or critical points;
  - monotonicity/invertibility regimes relevant to global extension.
- For each branch, record:
  - defining assumptions;
  - the actual object obtained;
  - whether it is globally admissible at the required scope;
  - status: retained / excluded / unresolved.
- You must NOT conclude "only trivial solutions", "no nontrivial solution", or any exact classification while any branch remains unresolved.
- If one branch is excluded because it hits a singularity or loses smoothness/invertibility, you must still check whether another sign/range of the same constant or another branch avoids that obstruction before excluding the whole family.

## Output Format

### Conjecture Block

Use `\begin{conjecture}` and `\end{conjecture}` to wrap your conjecture statement:

- Start with `[Brief description]` on the first line inside the tag.
- Follow with the pure mathematical statement, well-written in LaTeX.
- **Do NOT** include numbering or prefixes (e.g., "Lemma 1.", "Proposition", "Theorem").

### Proof Block

Use `\begin{proof}` and `\end{proof}` to wrap the proof:

- Reference existing lemmas as "Lemma X" or "Lemma-X" (e.g., "Lemma 1", "Lemma-35").

### Self-Containedness

Your conjecture statement must be self-contained unless a term is already defined in the given lemma list. Write it in a reusable, standalone form — if it passes verification, it becomes a lemma and will appear in future context.

### Example Output

\begin{conjecture}
[Anisotropic hypocoercive H^k a priori estimate for resistive MHD around a constant magnetic field] Pure mathematical statement well written in LaTeX.
\end{conjecture}

\begin{proof}
A rigorous and detailed proof. We will use Lemma 1 and Lemma-3 to establish our result.
\end{proof}
