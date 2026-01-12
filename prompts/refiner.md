## Instruction

You are an expert that is knowledgeable across all domains in math. This time you are asked to help with frontier math research. We have proposed a new conjecture, and tried to prove it. However, one reviewer has found some flaws in our proof. 

Your task is to refine the proof based on the review comments. When necessary, you may also modify the conjecture statement itself.

### Output Format

You should provide your refinement using XML tags:
- Use `<proof></proof>` to wrap your refined or rewritten proof
- Use `<conjecture></conjecture>` to wrap a modified conjecture statement (only when modification is necessary)

**Critical Requirement**: Your response must ONLY contain content within these XML tags. **Nothing else should be included** - no explanations, no introductions, no conclusions, no apologies, no text outside the tags, no conversational text. Just the XML tags with their content inside.

**Important**: You must provide at least one of `<proof>` or `<conjecture>` in your response. You may provide both if needed.

#### Example Output

```
<conjecture>
For any integer n > 2, the equation x^n + y^n = z^n has no non-trivial integer solutions.
</conjecture>

<proof>
We prove this by contradiction. Assume there exist positive integers x, y, z, n > 2 such that x^n + y^n = z^n. Without loss of generality, assume x, y, z are coprime... [rest of proof]
</proof>
```

### When to Modify the Conjecture Statement

You should modify the conjecture statement in the following situations:

1. **Weakening the statement**: If the current proof cannot support such a strong statement, consider weakening it to a smaller/weaker version, or extract a statement that corresponds to the part of the proof that is actually correct and provable.

2. **Negating the statement**: If you can confirm that the current statement is actually false, you may write a negated/opposite statement. The new statement should be **self-contained** and complete, unless the definitions have already appeared in the provided dependent lemmas.

3. **Extracting intermediate results (RECOMMENDED)**: If your refinement results in an overly lengthy proof, you may extract valuable intermediate results (e.g., useful calculations for future exploration or insights for subsequent research) as a new statement. The new statement must be accompanied by a new proof.

In all cases, the new statement MUST be **self-contained**.

### Guidelines for Refinement

- Your refined proof should be **correct**, **complete**, and **rigorous**
- Address all issues raised in the review
- If you modify the conjecture, make sure the new statement is clear and precise
- If you only fix the proof without changing the statement, you only need to provide `<proof></proof>`
- If you adjust the statement to match what can actually be proven, provide both `<conjecture></conjecture>` and `<proof></proof>`

## Conjecture

{conjecture_content}

## Proof

{proof_content}

## Review

{review_content}
