---
name: reddit-research
description: Search Reddit and read whole comment threads without an API key or login. Use when the user wants real opinions, first-hand experience or community consensus on a product, university, error, purchase or plan, and wants the links to prove it.
---

# Reddit research with reddit-rail

You have a CLI called `reddit`. It reads a public Reddit archive, so there is nothing to
sign up for. Every result carries a permalink: keep it and cite it.

## Ask a question (default)

```bash
reddit ask "<the user's question>" --subs <sub1>,<sub2> --threads 3 --quotes 5
```

This searches, opens the top threads, and prints the highest-scoring comments verbatim
with their score and permalink. Use this when the user wants opinions or experience.

Pick subreddits by domain, not by size:

- study, university, apprenticeships: `6thForm`, `UniUK`, `EngineeringStudents`, `AskUK`
- money, buying, careers: `UKPersonalFinance`, `cscareerquestionsEU`, `smallbusiness`
- software, AI: `programming`, `LocalLLaMA`, `selfhosted`, `MachineLearning`
- a product: its own sub, e.g. `bose`, `headphones`, `raspberry_pi`

Name any sub you need. Unknown subs register themselves on first use.

## Find threads

```bash
reddit search "<keywords>" --subs <sub1>,<sub2> --limit 20 --window year
```

## Read one thread

```bash
reddit thread <permalink or post id> --limit 60
```

## Rules

1. **Quote with the link.** Every claim you take from Reddit carries the permalink and the
   score it had when read. Reddit is anecdote, not evidence: say how many threads agreed.
2. **Name the subs you searched.** "r/6thForm and r/UniUK" is a finding; "Reddit says" is not.
3. **Do not over-read one comment.** A 100-point comment is one person. Look for the same
   claim in a second thread before treating it as consensus.
4. **Respect the archive's limits.** Commands pace themselves and cap requests. If a
   command reports `_skipped:_` or `_throttled:_`, report that to the user rather than
   retrying in a loop.
5. **No private data.** This reads public posts only. Do not try to de-anonymise authors.

## Machine-readable

`--json` on `search`, `ask` and `thread` returns one object. Prefer it when you need to
process the results rather than print them.
