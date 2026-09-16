# Vault bundle — 2026-09-15, Fools Gold mobile

These three files could not be written into `~/projects/ontology-vault` this
session: `~/projects` is not a connected folder and the access request was
refused by the session's approval layer, which a standing grant cannot waive.
They are prepared here so nothing was dropped silently.

**Check before committing them:**

1. **The log number.** `2026-09-15-042` is a guess. `2026-09-15-041` is known to
   exist (it is cited in the skill itself). Renumber to the real next value and
   fix the `provenance` references in the other two files to match.
2. **Property types.** `type: Claim` on the candidate and `type: Meta` on the
   prediction are the conservative choices, not verified ones. Check
   `01 Constitution/01.03 Property Namespace.md`. The namespace is frozen at 33
   and an unreserved property is a lint error.
3. **`domain: foolsgold`** — check it matches the vocabulary already in use.
4. **`verdict: rejected-and-reordered`** on the log entry is descriptive, not a
   value taken from a known enum.
5. **Do not read the prediction file to yourself before ruling on the
   candidate.** That is the whole mechanism.

Then: `python3 "01 Constitution/01.08 Scripts/distill.py"`,
`verify.py`, and commit as one action.
