## Problem Statement

\begin{problem}
{problem_content}
\end{problem}

## Task

Generate a conjecture and its corresponding proof. While the ultimate goal is to resolve the problem, you are only allowed to propose exactly one conjecture with its proof in this response. Your conjecture may build upon the given lemmas wrapped, but should be substantially different from any given lemma. 

### Remaining Lemma Budget

Across all future explorations, you have a remaining budget of **{remaining_lemma_quota}** conjecture(s). In this run, you must propose exactly **one** conjecture.

## Output Format

1. Use `\begin{conjecture}` and `\end{conjecture}` to wrap your conjecture statement. 
  - At the beginning, use `[]` to wrap a brief description of the conjecture.
  - Then, give the pure mathematical statement which must be well written in LaTeX.
  - Do **NOT** include any numbering or prefixes inside the tag (e.g., “Lemma 1.”, “Proposition”, “Conjecture”, “Theorem”)
2. Use `\begin{proof}` and `\end{proof}` to wrap the proof of the conjecture.
3. Use `\begin{dependency}` and `\end{dependency}` to wrap the JSON list of lemma that you used in your proof. Use an empty array `[]` when the conjecture does not depend on other lemmas. All lemmas listed in the dependency must be used in the proof; likewise, all lemmas used in the proof must be listed here.

### Example Output

\begin{conjecture}
[Anisotropic hypocoercive H^k a priori estimate for resistive MHD around a constant magnetic field] Pure mathematical statement well written in LaTeX. 
\end{conjecture}

\begin{proof}
A rigorous and detailed proof.
\end{proof}

\begin{dependency}[1,3,4]\end{dependency}


## Content Requirements

### 1. Independence and Completeness
Your conjecture statement must be self-contained (unless definitions already appear in the given list of Lemmas (if any)), represent concrete progress, and be substantively different from existing lemmas.

If your conjecture pass verification, it will become lemma, and its statement will be listed in the context in future run. Therefore, you should write your conjecture statement in a self-contained, reusable, and standalone form.

### 2. Proof Rigor
Your conjecture must be accompanied by a detailed, complete, and rigorous proof. You must explicitly write down every intermediate derivation step in the proof. 

**No external citations:** Do not justify steps by citing papers/books/URLs. Any nontrivial claim must be proved in the proof or be a standard fact stated explicitly with its conditions.

**Important:** Avoid vague shortcuts (e.g., “it is easy to see”). Fully justify every key step.

### 3. Exploration and Discovery
You are required to explore different approaches or directions that might advance progress toward the final goal. Identify and articulate one interesting finding from your exploration as a new conjecture. 

**Intermediate results (recommended):** Useful findings include:
- Useful calculations, formulas, or bounds that establish relationships between key quantities
- Partial results that solve special cases or simplified versions of the problem
- Insights or knowledge about the mathematical objects involved
- Computational techniques or transformations that may prove useful in future exploration
- Results that establish important properties, even if narrower in scope than the final goal

These serve as stepping stones for later rounds.

## Core Principles

**Correctness is ALWAYS the top priority.**

When the remaining lemma budget is sufficient relative to the problem's difficulty:
  - Feel free to explore any different creative directions that could advance the problem. 
  - Prioritize producing a *smaller but correct* conjecture with a fully rigorous proof rather than attempting something overly ambitious. 
  - Only if you are **100% certain** you fully resolve the original problem should you write a conjecture statement that fully address the given problem.
  - If there is any doubt, propose a smaller conjectures with rigorous proofs. This will serve as the stepstone for further exploration.
  - Do not regret proving an intermediate result—we will have future chances; establishing useful intermediate results first is often more important.