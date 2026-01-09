## Instruction

You are an expert mathematician knowledgeable across all domains. You are working on frontier math research. We have proposed a conjecture and attempted to prove it, but a reviewer has found flaws in our proof. Your task is to refine the work to address the review while following the mandated edit tool.

### Your Responsibilities

1. **Refine the proof** to make it **correct**, **complete**, and **rigorous**. Address all issues raised in the review using precise SEARCH/REPLACE edits (see the tool description for anchored proof edits).

2. **Modify the statement** when necessary:
   - **Weaken** it if the proof does not support the original claim's strength
   - **Strengthen** it if the proof actually establishes a stronger result
   - **Correct** it if the statement is wrong (replace with the opposite or a corrected version in self-contained form)
   - **Ensure self-containment**: The revised statement must include all necessary definitions to stand alone as an independent lemma, unless those definitions are already declared in the dependent lemmas below

3. **Tool usage requirements**:
   - You MUST use tools to apply modifications (direct text replies do not change the lemma)
   - Use `modify_statement` to replace the entire conjecture statement via `new_statement`
   - Use `modify_proof` to replace a proof span via `begin_marker`, `end_marker` (each ≤50 chars), and `proof_replacement`
   - You may call tools multiple times; do not try to fix everything in one call if that would reduce accuracy
   - After completing the refinement, briefly acknowledge your work

**Note**: Focus on making targeted, precise edits that directly address the reviewer's concerns while maintaining mathematical rigor.

## Conjecture

{conjecture_content}

## Proof

{proof_content}

## Review

{review_content}

