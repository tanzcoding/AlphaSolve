## Problem Statement

\begin{problem}
{problem_content}
\end{problem}

## Task

Generate a conjecture and its corresponding proof. While the ultimate goal is to resolve the problem, you are only allowed to propose exactly one conjecture with its proof in this response. Your conjecture may build upon the given lemmas, but must be substantively different from any existing lemma.

### Remaining Lemma Budget

Across all future explorations, you have a remaining budget of **{remaining_lemma_quota}** conjecture(s). In this run, you must propose exactly **one** conjecture.

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

### P3.5 — Branch Ledger for Case Splits and Classification Claims

- Whenever your reasoning produces multiple candidate branches, parameter families, sign choices, parity classes, root choices, boundary regimes, regularity classes, domain restrictions, or local-vs-global possibilities, you MUST maintain an internal **branch ledger** before committing to a final conjecture.
- The branch ledger must explicitly track, for each branch: (i) where it came from, (ii) the assumptions/scope for that branch, and (iii) whether it is **proved**, **refuted**, or **unresolved**.
- You may reject an entire family only if you have a uniform argument covering the whole family. Testing a few sample values or one subcase is NOT enough to reject the family.
- If a family might split into subfamilies (for example by parity, sign, branch choice, boundary behavior, regularity threshold, or domain of definition), you MUST consider those subfamilies explicitly instead of collapsing them into one bucket.
- If any nontrivial branch remains unresolved, do NOT state a universal classification or non-existence conclusion. Propose a narrower statement that matches the strongest conclusion actually justified by the ledger.
- The branch ledger is primarily an internal reasoning discipline; you do not need to print it explicitly unless it is needed to make the proof rigorous and complete.

### P3.6 — Scope Lock, Candidate Sets, and Final Conclusions

- Before proposing a conjecture, identify the exact **scope** of the target claim: for example local vs global, formal vs actual, existential vs classificatory, qualitative vs quantitative, restricted-domain vs full-domain, or low-regularity vs smooth/analytic.
- Your conjecture and proof must match that scope exactly. You MUST NOT present a result about a weaker scope as if it solved a stronger one.
- Distinguish clearly between:
  1. **candidate families / necessary conditions / exploratory possibilities**,
  2. **families rigorously ruled out**,
  3. **families rigorously retained**, and
  4. **families still unresolved**.
- A candidate set obtained from local analysis, formal manipulation, symbolic elimination, asymptotics, heuristics, numerical checks, or restricted-case reasoning is NOT automatically the final answer.
- You may state a complete classification, exact characterization, or universal impossibility claim only if every candidate family has been accounted for at the full scope required by the problem.
- If your current evidence only identifies candidates, necessary conditions, local obstructions, or partial branch eliminations, then state the strongest justified intermediate result instead of a final classification.

### P5 — Tool Calls Require Full Context

Tools cannot see your prior reasoning. When invoking a tool, always include all necessary context inline: relevant definitions, variable bindings, assumptions, and the specific sub-question you need answered. Never assume the tool has access to anything outside the current call.

---

### P4 — Budget Awareness

| Remaining budget | Strategy |
|-----------------|----------|
| > 5 | Explore freely; creative or ambitious directions are encouraged |
| 2 – 5 | Prefer directions with higher confidence; avoid speculative leaps |
| ≤ 1 | Prioritize the most direct path to resolving the problem; do not spend budget on peripheral results |

---

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
