# Student Studio Pro — Flask + SQLite

A personal CV portfolio project: student and assistant attendance, secondary school grade classification, groups/schedules, exams/grades, payments, reports, authentication, roles, and a responsive dark/light workspace.

## Run in PyCharm (Windows)
1. Extract this ZIP. In PyCharm choose File → Open and select the **StudentStudio_Pro** folder (the folder containing `app.py`).
2. Select your existing Python interpreter and install Flask: `py -m pip install -r requirements.txt` (or use the full path to your interpreter with `-m pip`).
3. Run `app.py`. Open **http://127.0.0.1:5050**.
4. On first launch, create an admin username and password (10+ characters) on the setup page. Keep them safe; they are stored as password hashes. Then sign in.
5. Add students (name, student phone, guardian phone, first/second/third secondary), groups, exams, payments and assistant accounts using the sidebar.

## Roles
**Admin**: manage students, assistants, groups, exam definitions, payments, reports, and user accounts; record attendance and grades.
**Assistant**: view student profiles, assistants, and groups; record attendance and grade scores. Cannot create/delete students or see financial data.

Assistant directory entries (for attendance) and assistant login accounts are **separate**: create assistant login credentials under User accounts as admin.

## WhatsApp
A floating button uses the configured number `+201551551647` by default. To change it, set environment variable `WHATSAPP_NUMBER` (digits only, with country code) in PyCharm Run → Edit Configurations → Environment variables, then restart.

## Existing database
This archive intentionally includes **no students' private records**. It creates `database.db` next to `app.py` on first run.
To migrate your own existing Student Studio records: **stop both servers**, back up your current `database.db` (copy it somewhere safe), and copy the older database into this project folder **before first launch**. New tables are added automatically; the existing students, assistants, and attendance data are preserved. The admin setup page appears when the users table is empty. Old students may have no parent phone until edited.

## Scope and important limitations
- Payments track amounts **received**; they do not automatically generate monthly invoices or calculate outstanding balances.
- Exam score entry is manual. Groups support schedules and enrollments; there is no timetable conflict detection.
- Dashboard's attendance visualization displays the latest 7 dates that have attendance records (not necessarily the latest 7 calendar days).
- This is a **local portfolio demo**. Do not publicly deploy it with `debug=True`; before any production deployment set a stable strong `SECRET_KEY`, use HTTPS, strengthen authorization and validation, create backups, and implement privacy controls (the app stores minors' contact details). Avoid uploading the database to GitHub.
- If you lose the only admin password, there is no password reset flow yet; keep a backup.
- Use fictional student details when sharing demo screenshots or the repository.


## QR attendance + new branding

- New SS geometric logo is `static/logo.svg` (also favicon).
- Existing student records get random QR identifiers on first start; new students receive one automatically.
- Open **Students → Student profile** to view/download a student's private QR image.
- Open **Attendance → QR scanner** to scan from a staff account. Choose the attendance date; a repeated scan for a student already present on that date is ignored.
- Camera scanning uses an external `html5-qrcode` JavaScript library from unpkg.com; it needs network access. Browser camera access normally works on `localhost` and HTTPS, not insecure network IP URLs. Manual QR data entry is provided as a fallback.
- QR codes are static and **can be photographed or shared**. Treat this as a convenience feature, not identity verification. Staff should verify the person holding the card; consider rotating QR codes if anti-proxy attendance is important.
- Attendance uniqueness is **per day** (existing database design), not per class/group.

Install dependencies in the project's Python interpreter: `python -m pip install -r requirements.txt`.
The ZIP does not include a real student database. Back up your existing `database.db` before migrating it to this new version.


## Parent notifications (new)

1. Sign in as admin → Exams & grades → choose an exam; save students' grades.
2. Review scores and parent phone numbers. Choose **Publish results** and confirm that parents consented to WhatsApp messages.
3. **Manual WhatsApp:** open **Parent notifications** and click **Open WhatsApp** for each student, click Send in WhatsApp yourself, then click **I sent it**. Opening a link is **not** counted as delivery.
4. **Automatic API:** configure a WhatsApp Business Platform account, approved Arabic template with **3 positional body variables**: student name, exam name, and `score/max_score`. Set environment variables `WA_ACCESS_TOKEN`, `WA_PHONE_NUMBER_ID`, `WA_TEMPLATE_NAME`, optionally `WA_TEMPLATE_LANGUAGE=ar` and `WA_API_VERSION=v23.0`. Publish in Automatic API mode to submit. **Submitting is not confirmed delivery**; a production service should verify deliveries via webhook and implement retry/idempotency using WhatsApp message IDs.
5. Messages intentionally omit percentages. Parent numbers should be Egyptian mobile numbers (`01...` or `+201...`). Invalid numbers are flagged. No real credentials are included in this ZIP.
6. Results are read-only after publishing. Admin can reopen to edit; previously submitted messages cannot be recalled. Unchanged results are not requeued, including after reopen. Changed results can create a new pending notification, so review carefully.

### Security and privacy

Only admin can publish, confirm, and initiate WhatsApp messages. Parent consent is required at publication; collect consent in your actual workflow. Do **not** commit database files, phone numbers, tokens or secrets. This version performs synchronous API calls, so production deployments should use a task queue, webhooks, retries and rate limiting. Use HTTPS in production; set a persistent SECRET_KEY. Disable debug mode unless explicitly needed. This is a local project/demo, not a production-ready bulk messaging platform.
