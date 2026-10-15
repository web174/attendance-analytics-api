Code Review: app/main.py
#	Location	Risk or defect	Correction / handling
1	compute_late_minutes	Late minutes must compare the punch instant in IST with the scheduled shift start; overnight shifts also need careful attendance-date handling.	Convert punch timestamps to IST before comparing with the scheduled shift start. Verify overnight-shift edge cases with tests.
2	compute_work_hours / round_half_up	Python's built-in round() uses ties-to-even behavior, which can differ from a business rule requiring half-up rounding.	Use Decimal with ROUND_HALF_UP for consistent decimal rounding.
3	compute_overtime_minutes	Overtime must respect the 30-minute threshold and correctly determine the shift-end date for overnight shifts.	Calculate the shift end using the attendance date and overnight rule; return overtime only when the duration reaches 30 minutes.
4	create_employee	A check-then-insert pattern alone is vulnerable to concurrent duplicate requests.	Enforce a unique index on emp_code and translate DuplicateKeyError to HTTP 409.
5	list_employees	Pagination must start at offset zero for page 1, and totals must apply the same filters as the result query.	Use skip = (page - 1) * page_size and count_documents(q). Validate page and page size.
6	/attendance/punch-in	Duplicate punch-ins can race; an unknown employee should not create a log.	Check employee existence, enforce the unique (emp_code, date) index, and return HTTP 409 for duplicates.
7	list_attendance	Fetching all matching documents into Python before sorting/pagination can consume excessive memory.	Apply MongoDB .sort(), .skip(), and .limit() before iterating results.
8	/health	A health response that does not contact MongoDB can report healthy while the database is down.	Run MongoDB's ping command and return HTTP 503 when the database is unreachable.
9	Monthly analytics parameters	The month validation pattern must accept months 01 through 12 only.	Use `r"^\d{4}-(0[1-9]
10	department_summary	pipeline is a list of aggregation stages, so accessing pipeline["$match"] raises a type error.	Update the first stage using pipeline[0]["$match"]["department"].
11	MongoDB update return values	find_one_and_update should explicitly request the updated document when the response serializes that document.	Use return_document=ReturnDocument.AFTER.


Considered and Not Automatically Treated as Defects
- String storage for dates and shift times: YYYY-MM-DD and HH:MM are suitable representations for calendar dates and wall-clock shift times when consistently validated. Punch timestamps should remain real datetimes rather than date strings.
- Index selection: Additional indexes should be driven by real query plans and workload measurements because each index adds write and storage overhead.
Verification
Run syntax compilation from the project root:
python -m py_compile app/main.py
Then run the application:
uvicorn app.main:app --reload
Syntax compilation checks Python parsing only; it does not prove that MongoDB is available or that every endpoint's business logic is correct. Test the endpoints against a running MongoDB instance.