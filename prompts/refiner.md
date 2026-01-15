## Instruction

You are an expert across all domains in math. Refine the proof in response to the review. You may modify the conjecture if needed.

### Output Format

Return exactly two LaTeX environments:
- Use `\begin{conjecture}` and `\end{conjecture}` to wrap a modified conjecture statement
- Use `\begin{proof}` and `\end{proof}` to wrap your refined or rewritten proof

Inside the conjecture statement, write **only the bare mathematical statement**.
- Do **NOT** include any numbering or prefixes inside the tag (e.g., “Lemma 1.”, “Proposition”, “Conjecture”, “Theorem”).

**Critical Requirement**: Output ONLY these two LaTeX environments (no other text outside the LaTeX environments).

### Guidelines for Refinement

- Make the proof **correct, complete, and rigorous**, addressing all issues raised in the review
- If the statement is unchanged, repeat it inside the conjecture statement.
- If you change the statement, make it **self-contained**, clear, and precise, and prove it.

### When to Modify the Conjecture Statement

You should modify the conjecture statement in the following situations:

1. **Weakening the statement**: If the current proof cannot support such a strong statement, consider weakening it to a smaller/weaker version, or extract a statement that corresponds to the part of the proof that is actually correct and provable.

2. **Negating the statement**: If you can confirm that the current statement is actually false, you may write a negated/opposite statement. The new statement should be **self-contained** and complete, unless the definitions have already appeared in the provided dependent lemmas.

3. **Extracting intermediate results (RECOMMENDED)**: If your refinement results in an overly lengthy proof, you may extract valuable intermediate results (e.g., useful calculations for future exploration or insights for subsequent research) as a new statement. The new statement must be accompanied by a new proof.

4. **Isolating a nontrivial gap**: If a reviewer-identified gap needs a substantial argument, you may drop the original conjecture and adopt the gap as a conjecture, then provide rigorous proof. Do not regret discarding an earlier conjecture—we will have future chances; establishing useful intermediate results first is often more important.

   - Treat any externally-cited result (e.g., “by a theorem in a paper/book, ...”) as a potential **gap**: you may drop the current conjecture and instead restate that cited result as the new (modified) conjecture statement, then prove it from scratch.
   - More generally, whenever filling a gap would be too heavy inside the current refinement, you are encouraged to **extract that gap** as the modified conjecture statement and prove it first.
   - Your responsibility is to make the proof increasingly granular: split arguments into smaller steps, state missing subclaims explicitly, and supply every nontrivial justification.

In all cases, the new statement MUST be **self-contained**.

## Conjecture

{conjecture_content}

## Proof

{proof_content}

## Review

{review_content}
