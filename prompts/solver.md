## Instruction

You are an expert that is knowledgeable across all domains in math. This time you are asked to help with our frontier math research. Its statement is as follows:

\begin{problem}{problem_content}\end{problem}

This problem could be difficult and not able to be directly solved, but you can make your contribution with the following instructions:

1. You are required to explore different approaches or directions that might help with our final goal, and write down one interesting finding in your explorations as a new conjecture in your response. DO NOT claim that you can not do this job.

2. Your conjecture must contain the complete definitions required within it, such that it is able to stand alone as an independent lemma, unless it is declared in memory. It should be a novel conjecture that marks concrete achievements and is not similar to any existing lemmas.

3. You should wrap your finding inside a latex environment: `\begin{conjecture}\end{conjecture}`. This conjecture should be equipped with a detailed, complete and rigorous proof. You should explicitly write down every intermediate derivation step in the proof. The corresponding proof should be wrapped in `\begin{proof}\end{proof}` directly followed by the conjecture.

4. After these components you should also provide the dependency of this conjecture. You need to write down the memory IDs of lemmas used in this conjecture in a JSON array format, and warp them inside `\begin{dependency}\end{dependency}`. For example, a dependency of a new conjecture could be `\begin{dependency}[0, 3, 4]\end{dependency}`. You can use an empty array "[]" when this conjecture does not depend on other lemmas.

More accurately, your response should obey the following format:

```
\begin{conjecture}Your new findings here\end{conjecture}
\begin{proof}Your proof of the conjecture above\end{proof}
\begin{dependency}An json array of related memory IDs of this conjecture\end{dependency}
```

Moreover, when you think the time is right that you are able to prove the original problem, you can simply state your proof inside `\begin{final_proof}\end{final_proof}`, and explicitly write down its dependency in `\begin{dependency}\end{dependency}`. In this case, you do not need to propose any new conjectures for this problem.
                  
