## Task

Given a conjecture, its proof, and a review of that proof, refine the proof. You may also modify the conjecture if needed.

## Output Format

Return exactly two LaTeX environments:
1. Use `\begin{conjecture}` and `\end{conjecture}` to wrap the conjecture statement.
   - At the beginning, use `[]` to provide a brief description of the conjecture.
   - Then give a pure mathematical statement, written clearly in LaTeX.
   - Do **NOT** include any numbering or prefixes inside the environment (e.g., “Lemma 1.”, “Proposition”, “Conjecture”, “Theorem”).
2. Use `\begin{proof}` and `\end{proof}` to wrap the proof.

Output ONLY these two LaTeX environments (no other text outside the LaTeX environments).

### Example Output

\begin{conjecture}
[Anisotropic hypocoercive H^k a priori estimate for resistive MHD around a constant magnetic field] Pure mathematical statement well written in LaTeX. 
\end{conjecture}

\begin{proof}
Complete and detailed proof.
\end{proof}

## Guidelines for Refinement

- Address every issue raised in the review, while still producing a **complete** proof of the conjecture.
- If the statement is unchanged, repeat it inside the conjecture statement.
- If you change the statement, make it **self-contained**, clear, and precise, and prove it.

### When to Modify the Conjecture Statement

You should modify the conjecture statement in the following situations:

1. **Weakening the statement**: If the current proof cannot support such a strong statement, consider weakening it to a smaller/weaker version, or extract a statement that corresponds to the part of the proof that is actually correct and provable.

2. **Negating the statement**: If you can confirm that the current statement is actually false, you may write a negated/opposite statement. The new statement should be **self-contained** and complete, unless the definitions have already appeared in the provided dependent lemmas.

3. **Isolating a nontrivial argument**: If an argument in the original proof is highly technical, lengthy, and  needs repairment, you are required to drop the original conjecture and adopt the argument as a conjecture, then provide rigorous proof. Do not regret discarding an earlier conjecture—we will have future chances; establishing useful intermediate results first is often more important.

   - Treat any externally-cited result (e.g., “by a theorem in a paper/book, ...”) as a potential **gap**: you may drop the current conjecture and instead restate that cited result as the new (modified) conjecture statement, then prove it from scratch.
   - More generally, whenever an existing technical argument is already too lengthy or repairing a gap is too heavy inside the current refinement, you are required to drop the current conjecture, **extract that gap or argument** as the new conjecture statement, and prove it first.

In all cases, the new statement MUST be **self-contained**. The corresponding new proof MUST be **COMPLETE**.

## Conjecture

{conjecture_content}

## Proof

{proof_content}

## Review

{review_content}
