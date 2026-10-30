devflow task $task_id in project `$project` was approved by the human and is done.
Branch `$branch` holds the change (not merged, not pushed).

Read the task with `task_read` (task_id `$task_id`). Then record what is worth
remembering — and only that. Most tasks need one decision entry and nothing else.

1. `decision_add(project="$project", title=..., body=...)` — only if the task made a
   choice someone would later ask "why?" about. Body uses this shape:

   **Context:** ...

   **Decision:** ...

   **Alternatives considered:** ...

   **Consequences:** ... (mention branch `$branch` and task [[$task_id]])

2. `project_note_update(project="$project", section=..., text=...)` — only for a
   durable fact about the project learned during the task (a gotcha, a hidden
   dependency). Append to an existing section where one fits.

3. `task_append_log(task_id="$task_id", section="Result", text=...)` — two or three
   lines: what changed, and what the human should check before merging.

Reply with one line saying what you recorded.
