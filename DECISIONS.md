# DECISIONS.md

1. **Indexes.** employees: unique `emp_code` (no duplicates, joins), `department` (summary and trend filter), `joined_on` (headcount). attendance_logs: unique `(emp_code, date)` (one record per day, punch-out, monthly report), `(date desc, emp_code)` (list sort, leaderboard, trend). Rejected: an index on `status`, it has only five values and narrows nothing.

2. **Punch-in race.** Both requests call insert_one. The unique `(emp_code, date)` index lets one succeed; the other gets DuplicateKeyError, which I return as 409. There is no check-then-insert gap.

3. **Ties.** `$setWindowFields` with `$rank` gives tied employees the same rank and skips the next one. The limit is applied after ranking (`rank <= limit`), so tied rows at the cutoff are all returned, ordered by emp_code.

4. **Headcount.** The pipeline starts from employees, keeps those with `joined_on` on or before the month end, then `$lookup`s their logs. Each employee counts once, so people with zero logs are counted and later joiners are not.

5. **100x data.** Leaderboard and summary scan a whole month of logs on every request. I would store pre-aggregated totals per employee and month, updated at punch-in, punch-out and PATCH.
