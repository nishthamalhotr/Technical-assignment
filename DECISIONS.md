# Design decisions

1. **Indexes.** A unique `employees.emp_code` index enforces the customer-assigned identity. A unique `(emp_code, date)` attendance index is both the natural-key guarantee and the punch-in race guard. Attendance indexes on `(date, emp_code)`, `(status, date, emp_code)`, and employee/date support filtered lists, month scans, and trend lookups. Employee indexes on department/join date and join date/department support department analytics and explain plans. I did not add an email index: the API neither looks up employees by email nor promises unique email addresses.

2. **Punch-in race.** Both requests may read the employee and compute the same attendance date. Each then inserts that natural key. MongoDB's unique index commits one insert; the other receives `DuplicateKeyError`, which the route maps to 409. There is no application-level check-then-insert guarantee.

3. **Ties.** Ranking uses MongoDB's `$rank` over total late minutes, before applying the cutoff. Employees tied at a rank no greater than `limit` all appear; rows are then ordered by minutes descending and code ascending. The response may exceed `limit`.

4. **Headcount.** Department summary starts from eligible employee documents and only then looks up logs. Employees with no records therefore remain in the group; employees joining after month-end are filtered out.

5. **100x scale.** I would partition analytics by month, benchmark indexes against production distributions, and consider pre-aggregated daily summaries. Those changes need workload measurements and freshness requirements first.
