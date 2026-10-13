# Code Review — Employee Attendance & Analytics API

## Defects Found

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `punch_out()` — datetime calculation | MongoDB may return a naive UTC datetime, while the punch-out timestamp is timezone-aware. Subtracting them raises a `TypeError`. | Call `POST /attendance/punch-out` for an employee with an open attendance record. The API may return `500 Internal Server Error` with a naive/aware datetime subtraction error. | Normalize the stored punch-in datetime to UTC when its timezone is missing, ensure the punch-out timestamp is timezone-aware, and use the normalized datetime for work-duration calculations. |

## Reviewed but Not Considered Defects

1. Duplicate punch-in requests returning `409 Conflict` are intentional because an employee should not create a duplicate attendance record for the same attendance date.
2. Duplicate punch-out requests returning `409 Conflict` are intentional because an attendance record should not be punched out more than once.
3. A `404 Not Found` response at `/` is not necessarily a defect because the API documentation is available at `/docs`.

## Review Notes

The datetime issue above was observed during local testing. Duplicate-request handling was also tested and returned the expected conflict responses.

This review records the confirmed issue described above. Additional defects should be documented only after checking the original starter code against the requirements in `openapi.yaml` (R1–R10) and verifying them with appropriate tests.