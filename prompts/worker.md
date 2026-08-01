You are reviewing and fixing code-review suggestions for exactly ONE file:

    {file}

Below is the review text for this file, verbatim. It may contain several separate
suggestions. Treat each distinct suggestion on its own.

--- REVIEW ---
{findings}
--- END REVIEW ---

## Step 1 - Analyse every suggestion FIRST (do NOT edit code yet)

Before proposing or applying any change, for EACH suggestion work through this and write it
out explicitly:

1. Restate the suggestion in your own words and explain what it means in practical terms.
2. Explain the CURRENT behavior:
   - What code is involved
   - What actually happens at runtime
   - Under what conditions the behavior appears
3. Explain the PROPOSED FIX:
   - What would change in the code
   - What behavior it is trying to alter or prevent
4. Explain the RESULT AFTER THE FIX:
   - What the user would see differently
   - Any side effects or behavior changes
5. Evaluate NECESSITY:
   - Is this actually a bug, a visual inconsistency, or an intentional design choice?
   - Is the current behavior incorrect or just different?
   - Under what assumptions would the fix be required vs not required?
6. Identify RISKS:
   - Could this break other states, themes, or components?
   - Is the change scoped or global?
7. Conclude with a recommendation:
   - Apply the fix / Do not apply / Needs more context
   - Clearly state why.

If assumptions are required, list them explicitly. Do NOT modify code until this analysis
is complete for every suggestion.

## Step 2 - Apply only what you recommended

After the analysis is finished for all suggestions:
- Apply the ones you concluded **Apply**. Edit ONLY `{file}`.
- Do NOT apply the ones you concluded **Do not apply** or **Needs more context**.

Rules:
1. Edit ONLY `{file}`. Do not touch or create any other file.
2. Keep each change minimal and scoped to its suggestion; do not refactor unrelated code.
3. Any recommendation that depends on another file, shared configuration, or a
   cross-file contract must be concluded **Needs more context**. Do not apply partial
   edits for it; list it under "skipped" together with the dependency it relies on.
4. Do NOT run git. Do not commit, stage, or push. Leave the file edited in place.
5. Do NOT add comments that mention code review, AI, automation, or where a suggestion
   came from.

## Step 3 - Summary (must be the LAST thing in your response)

Keep your full Step-1 analysis in the response above - it is the record of your reasoning.
Then end with ONLY this fenced json block and nothing after it:

```json
{
  "file": "{file}",
  "applied": [
    { "summary": "<plain sentence: what changed and why it matters>" }
  ],
  "skipped": [
    { "finding": "<short description of the suggestion>", "reason": "<Do not apply / Needs more context - why, in one line>" }
  ]
}
```

For each "applied" summary, use a developer's own voice - say what was fixed or improved.
Do NOT mention tools, AI, "review", "suggestions", or where it came from. Examples:
  - "Fixed a crash on the settings page when a user had no saved profile."
  - "Stopped the export button from doing nothing on an empty list."
  - "Tightened validation on the API-key form so bad values are caught before saving."

If you applied nothing, return an empty "applied" list and put everything under "skipped".
