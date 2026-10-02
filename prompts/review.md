You are reviewing devflow task $task_id in project `$project`. You wrote the spec;
a cheaper model implemented it in this worktree on branch `$branch`. This is
review round $round of at most $max_rounds.

Read the spec with `task_read` (task_id `$task_id`), then the change:
`git diff $base...HEAD` (and `git log $base..HEAD`). Read the surrounding code
where the diff alone doesn't tell you whether it is right.

The bot's verification evidence:

$evidence

Judge:
1. Does it do what the spec says — all of it, and nothing else?
2. Is it correct? Look for broken edge cases, SQL injection / XSS in CodeIgniter
   code paths, missing escaping, wrong query builder use, PHP version
   incompatibilities.
3. Did verification fail, and is that the change's fault?

Write your review with `task_append_log(task_id="$task_id", section="Review",
text=...)`: a "Round $round" heading, then findings as bullets with file:line.

## Your final message

Either exactly:

VERDICT: APPROVE

or:

VERDICT: FIXUP
<a fix-up spec for the worker: numbered, concrete, file by file>
