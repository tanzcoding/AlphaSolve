## Task

Given a conjecture, its incomplete/problematic proof, and a review of that proof, extract the parts from the proof that you have absolute confidence are correct. This is the final extraction attempt - do NOT try to fix or repair the proof. Only extract what is definitely correct.

## Output Format

Return exactly two LaTeX environments:
1. Use `\begin{conjecture}` and `\end{conjecture}` to wrap the extracted correct statement.
   - At the beginning, use `[]` to provide a brief description.
   - Then give the mathematical statement, written clearly in LaTeX.
   - Do **NOT** include any numbering or prefixes inside.
2. Use `\begin{proof}` and `\end{proof}` to wrap the extracted correct proof steps.

Output ONLY these two LaTeX environments (no other text outside).

### Example Output

\begin{conjecture}
[Extracted correct partial result] Pure mathematical statement well written in LaTeX. 
\end{conjecture}

\begin{proof}
The parts of the proof that are definitely correct.
\end{proof}

## Guidelines for Extraction

- **ONLY EXTRACT WHAT IS DEFINITELY CORRECT**: Be extremely conservative. If there's any doubt about a step, exclude it.
- **DO NOT TRY TO FIX OR REPAIR**: This is not a revision task. Do not attempt to fill gaps or fix errors.
- **It's acceptable to extract less**: It's better to have a small, definitely correct result than a larger one with potential errors.
- **If nothing is definitely correct**: You may return an empty conjecture and proof, but try to extract at least some minimal correct content if possible.

## What to Extract

Look for:
1. Initial definitions and setup that are clearly correct
2. Simple algebraic manipulations that are obviously valid
3. Steps that follow directly from verified context lemmas
4. Any intermediate results that are rigorously established

## What to Exclude

Exclude:
1. Any step with logical gaps
2. Steps that rely on unproven assumptions
3. Calculations that might contain errors
4. Reasoning that is unclear or ambiguous
5. Any part that was criticized in the review

## Conjecture

{conjecture_content}

## Proof

{proof_content}

## Review

{review_content}
