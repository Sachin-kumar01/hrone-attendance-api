# Engineering Decisions

## 1. Indexes
I used indexes to make attendance lookups faster and help prevent duplicate attendance records for the same employee and date. A unique compound index on employee ID and attendance date is useful for enforcing this rule at the database level. I would consider an index on department if department-based summaries become slow, but I would add it only after checking query performance.

## 2. Punch-in race
If two identical punch-in requests arrive at the same time, both may initially check that no attendance record exists. The unique database constraint allows only one insert to succeed. The other request should return a conflict response instead of creating a duplicate record.

## 3. Ties
If employees have equal late minutes at the leaderboard cutoff, the API returns only the requested limit, using a consistent secondary sort order to decide which tied employees appear.

## 4. Headcount
The department summary should count employees from the employees collection first and then combine their attendance statistics. This ensures employees with zero attendance logs are still included.

## 5. Scaling
For 100 times more data, I would review query execution plans, add indexes based on actual query patterns, and move expensive analytics to aggregation pipelines or precomputed summaries where appropriate.