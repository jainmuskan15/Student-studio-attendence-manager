"""Student Studio: local Flask student and attendance manager."""
import csv
import io
import hashlib
import qrcode
import os
import secrets
import sqlite3
import json
import urllib.request
import urllib.error
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get(
    'STUDENT_STUDIO_DB',
    '/tmp/student_studio_demo.db'
    if os.environ.get('VERCEL')
    else BASE_DIR / 'database.db'
))

GRADES = [
    ('1', 'First secondary'),
    ('2', 'Second secondary'),
    ('3', 'Third secondary')
]
GRADE_LABELS = dict(GRADES)
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
WHATSAPP_NUMBER = ''.join(
    ch for ch in os.environ.get('WHATSAPP_NUMBER', '')
    if ch.isdigit()
)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax'
)

def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


@app.teardown_appcontext
def close_db(_error):
    conn = g.pop('db', None)
    if conn:
        conn.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('''CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            phone TEXT, grade_level TEXT, parent_phone TEXT DEFAULT ''
        )''')
        cols = {row[1] for row in conn.execute('PRAGMA table_info(students)')}
        if 'parent_phone' not in cols:
            conn.execute("ALTER TABLE students ADD COLUMN parent_phone TEXT DEFAULT ''")
        if 'qr_token' not in cols:
            conn.execute('ALTER TABLE students ADD COLUMN qr_token TEXT')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_students_qr_token ON students(qr_token)')
        # Upgrade existing students without changing their IDs or attendance history.
        for (student_id,) in conn.execute("SELECT id FROM students WHERE qr_token IS NULL OR qr_token = ''").fetchall():
            conn.execute('UPDATE students SET qr_token=? WHERE id=?', (secrets.token_urlsafe(24), student_id))
        conn.execute('''CREATE TABLE IF NOT EXISTS assistants (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT DEFAULT ''
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_type TEXT NOT NULL CHECK(person_type IN ('student','assistant')),
            person_id INTEGER NOT NULL, attendance_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('present','absent','late')),
            UNIQUE(person_type, person_id, attendance_date)
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance(attendance_date, person_type)')
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','assistant')));
        CREATE TABLE IF NOT EXISTS study_groups (id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, grade_level TEXT NOT NULL, meeting_day TEXT DEFAULT '',
            meeting_time TEXT DEFAULT '', room TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS enrollments (student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            group_id INTEGER NOT NULL REFERENCES study_groups(id) ON DELETE CASCADE,
            PRIMARY KEY(student_id,group_id));
        CREATE TABLE IF NOT EXISTS exams (id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL, grade_level TEXT NOT NULL, exam_date TEXT NOT NULL,
            max_score REAL NOT NULL CHECK(max_score > 0));
        CREATE TABLE IF NOT EXISTS exam_scores (exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            score REAL NOT NULL CHECK(score >= 0), PRIMARY KEY(exam_id,student_id));
        CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            amount REAL NOT NULL CHECK(amount > 0), paid_on TEXT NOT NULL,
            month TEXT NOT NULL, notes TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS idx_payment_student ON payments(student_id,month);
        """)
        exam_cols={r[1] for r in conn.execute('PRAGMA table_info(exams)')}
        if 'published_at' not in exam_cols:
            conn.execute('ALTER TABLE exams ADD COLUMN published_at TEXT')
        conn.execute('''CREATE TABLE IF NOT EXISTS parent_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            score REAL NOT NULL, phone TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            last_error TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(exam_id, student_id))''')
        conn.commit()
    finally:
        conn.close()


def current_user():
    uid = session.get('uid')
    if not uid: return None
    return db().execute('SELECT id,username,role FROM users WHERE id=?', (uid,)).fetchone()


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user(): return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapped


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user: return redirect(url_for('login'))
        if user['role'] != 'admin': abort(403)
        return fn(*args, **kwargs)
    return wrapped


@app.before_request
def check_auth():
    endpoint = request.endpoint or ''
    if endpoint == 'static': return
    user_count = db().execute('SELECT COUNT(*) FROM users').fetchone()[0]
    if user_count == 0:
        if endpoint not in ('setup',): return redirect(url_for('setup'))
    elif endpoint == 'setup': return redirect(url_for('home'))
    elif endpoint not in ('login', 'logout', 'contact') and not current_user():
        return redirect(url_for('login'))


@app.route('/setup', methods=['GET','POST'])
def setup():
    if db().execute('SELECT COUNT(*) FROM users').fetchone()[0]: return redirect(url_for('home'))
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        if len(username) < 3 or len(username) > 40 or len(password) < 10:
            flash('Username: 3–40 characters. Password: at least 10 characters.', 'error')
        else:
            db().execute('INSERT INTO users(username,password_hash,role) VALUES(?,?,?)', (username,generate_password_hash(password),'admin'))
            db().commit()
            flash('Admin account created. Sign in to continue.', 'success')
            return redirect(url_for('login'))
    return render_template('auth.html',mode='setup')


@app.route('/login',methods=['GET','POST'])
def login():
    if current_user(): return redirect(url_for('home'))
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        user = db().execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
        if user and check_password_hash(user['password_hash'],password):
            session.clear()
            session['uid'] = user['id']
            return redirect(url_for('home'))
        flash('Invalid username or password.', 'error')
    return render_template('auth.html',mode='login')


@app.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.before_request
def protect_posts():
    if request.method == 'POST':
        if not secrets.compare_digest(session.get('csrf_token', ''), request.form.get('csrf_token', '')):
            abort(400, 'Invalid form token. Refresh the page and try again.')


@app.context_processor
def shared():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)
    return {'csrf_token': session['csrf_token'], 'grades': GRADES,
            'grade_labels': GRADE_LABELS, 'whatsapp_number': WHATSAPP_NUMBER,
            'today': date.today().isoformat(), 'user': current_user()}


def normalized_grade(value):
    value = (value or '').strip()
    aliases = {'1st': '1', '2st': '2', '2nd': '2', '3rd': '3',
               'first': '1', 'second': '2', 'third': '3',
               'first secondary': '1', 'second secondary': '2', 'third secondary': '3',
               'grade 10': '1', 'grade 11': '2', 'grade 12': '3'}
    return aliases.get(value.lower(), value)


def validate_student(form):
    name = form.get('name', '').strip()
    phone = form.get('phone', '').strip()
    parent_phone = form.get('parent_phone', '').strip()
    grade = normalized_grade(form.get('grade_level'))
    if not name or not phone or not parent_phone:
        return None, 'Name, student phone and parent phone are required.'
    if grade not in GRADE_LABELS:
        return None, 'Choose a valid secondary grade.'
    if len(name) > 120 or any(len(p) > 24 for p in (phone, parent_phone)):
        return None, 'One of the fields is too long.'
    return (name, phone, parent_phone, grade), None


def redirect_back(default):
    # Only allow internal paths (never user-provided remote redirects).
    path = request.form.get('next', '')
    return redirect(path if path.startswith('/') and not path.startswith('//') else url_for(default))


@app.get('/')
def home():
    conn = db()
    student_total = conn.execute('SELECT COUNT(*) FROM students').fetchone()[0]
    assistant_total = conn.execute('SELECT COUNT(*) FROM assistants').fetchone()[0]
    attendees = conn.execute('''SELECT person_type, status, COUNT(*) AS n FROM attendance
        WHERE attendance_date = ? GROUP BY person_type,status''', (date.today().isoformat(),)).fetchall()
    today_present = sum(r['n'] for r in attendees if r['status'] == 'present')
    grade_counts = {grade: conn.execute('SELECT COUNT(*) FROM students WHERE grade_level = ?', (grade,)).fetchone()[0] for grade, _ in GRADES}
    latest = conn.execute('SELECT * FROM students ORDER BY id DESC LIMIT 6').fetchall()
    payments_total = conn.execute('SELECT COALESCE(SUM(amount),0) FROM payments').fetchone()[0]
    exam_total = conn.execute('SELECT COUNT(*) FROM exams').fetchone()[0]
    attendance_trend = conn.execute("""SELECT attendance_date,
        SUM(CASE WHEN status='present' THEN 1 ELSE 0 END) AS present,
        COUNT(*) AS total FROM attendance WHERE person_type='student'
        GROUP BY attendance_date ORDER BY attendance_date DESC LIMIT 7""").fetchall()
    return render_template('dashboard.html', payments_total=payments_total,
                           exam_total=exam_total, attendance_trend=list(reversed(attendance_trend)), active='dashboard', student_total=student_total,
                           assistant_total=assistant_total, today_present=today_present,
                           grade_counts=grade_counts, latest=latest)


@app.get('/students')
def students():
    q = request.args.get('q', '').strip()
    grade = request.args.get('grade', '').strip()
    clauses, params = [], []
    if q:
        clauses.append('(name LIKE ? OR phone LIKE ? OR parent_phone LIKE ?)')
        params.extend([f'%{q}%'] * 3)
    if grade in GRADE_LABELS:
        clauses.append('grade_level = ?')
        params.append(grade)
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    rows = db().execute('SELECT * FROM students' + where + ' ORDER BY id DESC', params).fetchall()
    total = db().execute('SELECT COUNT(*) FROM students').fetchone()[0]
    return render_template('students.html', active='students', students=rows, total=total, q=q, grade=grade)


@app.post('/students/add')
@admin_required
def add_student():
    values, error = validate_student(request.form)
    if error:
        flash(error, 'error')
    else:
        db().execute('INSERT INTO students(name,phone,parent_phone,grade_level,qr_token) VALUES (?,?,?,?,?)', (*values, secrets.token_urlsafe(24)))
        db().commit()
        flash('Student added.', 'success')
    return redirect(url_for('students'))


@app.route('/students/<int:student_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_student(student_id):
    row = db().execute('SELECT * FROM students WHERE id=?', (student_id,)).fetchone()
    if not row:
        abort(404)
    if request.method == 'POST':
        values, error = validate_student(request.form)
        if error:
            flash(error, 'error')
        else:
            db().execute('UPDATE students SET name=?,phone=?,parent_phone=?,grade_level=? WHERE id=?', (*values, student_id))
            db().commit()
            flash('Student updated.', 'success')
            return redirect(url_for('students'))
    return render_template('edit_student.html', active='students', student=row)


@app.post('/students/<int:student_id>/delete')
@admin_required
def delete_student(student_id):
    db().execute("DELETE FROM attendance WHERE person_type='student' AND person_id=?", (student_id,))
    db().execute('DELETE FROM students WHERE id=?', (student_id,))
    db().commit()
    flash('Student deleted.', 'success')
    return redirect(url_for('students'))


@app.get('/assistants')
def assistants():
    rows = db().execute('SELECT * FROM assistants ORDER BY id DESC').fetchall()
    return render_template('assistants.html', active='assistants', assistants=rows)


@app.post('/assistants/add')
@admin_required
def add_assistant():
    name, phone = request.form.get('name', '').strip(), request.form.get('phone', '').strip()
    if not name or len(name) > 120 or len(phone) > 24:
        flash('Enter a valid assistant name and phone.', 'error')
    else:
        db().execute('INSERT INTO assistants(name,phone) VALUES (?,?)', (name, phone))
        db().commit()
        flash('Assistant added.', 'success')
    return redirect(url_for('assistants'))


@app.post('/assistants/<int:assistant_id>/delete')
@admin_required
def delete_assistant(assistant_id):
    db().execute("DELETE FROM attendance WHERE person_type='assistant' AND person_id=?", (assistant_id,))
    db().execute('DELETE FROM assistants WHERE id=?', (assistant_id,))
    db().commit()
    flash('Assistant removed.', 'success')
    return redirect(url_for('assistants'))


def valid_day(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date().isoformat()
    except (ValueError, TypeError):
        return date.today().isoformat()


@app.get('/attendance/<person_type>')
def attendance(person_type):
    if person_type not in ('student', 'assistant'):
        abort(404)
    day = valid_day(request.args.get('date'))
    grade = request.args.get('grade', '') if person_type == 'student' else ''
    table = 'students' if person_type == 'student' else 'assistants'
    query = f'SELECT id,name,phone FROM {table}'
    params = []
    if grade in GRADE_LABELS and person_type == 'student':
        query += ' WHERE grade_level = ?'
        params.append(grade)
    query += ' ORDER BY name COLLATE NOCASE'
    people = db().execute(query, params).fetchall()
    recorded = db().execute('SELECT person_id,status FROM attendance WHERE person_type=? AND attendance_date=?', (person_type, day)).fetchall()
    statuses = {r['person_id']: r['status'] for r in recorded}
    count = {s: sum(statuses.get(p['id']) == s for p in people) for s in ('present','absent','late')}
    return render_template('attendance.html', active='attendance_' + person_type, person_type=person_type,
                           people=people, statuses=statuses, counts=count, selected_date=day, grade=grade)


@app.post('/attendance/<person_type>/save')
def save_attendance(person_type):
    if person_type not in ('student', 'assistant'):
        abort(404)
    day = valid_day(request.form.get('date'))
    grade = request.form.get('grade', '') if person_type == 'student' else ''
    table = 'students' if person_type == 'student' else 'assistants'
    valid_ids = {r['id'] for r in db().execute(f'SELECT id FROM {table}')}
    for key, status in request.form.items():
        if not key.startswith('status_'):
            continue
        try:
            person_id = int(key[7:])
        except ValueError:
            continue
        if person_id not in valid_ids:
            continue
        if status == '':
            db().execute('DELETE FROM attendance WHERE person_type=? AND person_id=? AND attendance_date=?', (person_type, person_id, day))
        elif status in ('present', 'absent', 'late'):
            db().execute('''INSERT INTO attendance(person_type,person_id,attendance_date,status)
                VALUES(?,?,?,?) ON CONFLICT(person_type,person_id,attendance_date)
                DO UPDATE SET status=excluded.status''', (person_type, person_id, day, status))
    db().commit()
    flash('Attendance saved for ' + day + '.', 'success')
    return redirect(url_for('attendance', person_type=person_type, date=day, grade=grade))


@app.get('/export/students')
@admin_required
def export_students():
    rows = db().execute('SELECT id,name,phone,parent_phone,grade_level FROM students ORDER BY id').fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Student name', 'Phone', 'Parent phone', 'Secondary grade'])
    writer.writerows([r['id'],r['name'],r['phone'],r['parent_phone'],GRADE_LABELS.get(r['grade_level'],r['grade_level'])] for r in rows)
    return Response('\ufeff' + output.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename=student_studio_students.csv'})


@app.get('/contact')
def contact():
    message = quote('Hello, I would like to ask about the student system.')
    return redirect(f'https://wa.me/{WHATSAPP_NUMBER}?text={message}' if WHATSAPP_NUMBER else f'https://api.whatsapp.com/send?text={message}')



@app.get('/students/<int:student_id>')
def student_profile(student_id):
    student = db().execute('SELECT * FROM students WHERE id=?',(student_id,)).fetchone()
    if not student: abort(404)
    attendance_rows = db().execute("SELECT attendance_date,status FROM attendance WHERE person_type='student' AND person_id=? ORDER BY attendance_date DESC LIMIT 30",(student_id,)).fetchall()
    scores = db().execute('''SELECT e.title,e.exam_date,e.max_score,s.score FROM exam_scores s
        JOIN exams e ON e.id=s.exam_id WHERE s.student_id=? ORDER BY e.exam_date DESC''',(student_id,)).fetchall()
    memberships = db().execute('''SELECT g.* FROM study_groups g JOIN enrollments e ON g.id=e.group_id
        WHERE e.student_id=? ORDER BY g.name''',(student_id,)).fetchall()
    payments = db().execute('SELECT * FROM payments WHERE student_id=? ORDER BY paid_on DESC',(student_id,)).fetchall() if current_user()['role']=='admin' else []
    return render_template('profile.html',active='students',student=student,attendance_rows=attendance_rows,
                           scores=scores,memberships=memberships,payments=payments)


@app.get('/groups')
def groups():
    rows=db().execute('''SELECT g.*,COUNT(e.student_id) AS members FROM study_groups g
        LEFT JOIN enrollments e ON e.group_id=g.id GROUP BY g.id ORDER BY g.grade_level,g.name''').fetchall()
    return render_template('groups.html',active='groups',groups_rows=rows)


@app.post('/groups/add')
@admin_required
def add_group():
    name=request.form.get('name','').strip()
    grade=request.form.get('grade_level','')
    day=request.form.get('meeting_day','').strip()[:24]
    time=request.form.get('meeting_time','').strip()[:16]
    room=request.form.get('room','').strip()[:80]
    if not name or len(name)>100 or grade not in GRADE_LABELS:
        flash('Enter a group name and valid grade.', 'error')
    else:
        db().execute('INSERT INTO study_groups(name,grade_level,meeting_day,meeting_time,room) VALUES(?,?,?,?,?)',(name,grade,day,time,room))
        db().commit();flash('Group created.','success')
    return redirect(url_for('groups'))


@app.route('/groups/<int:group_id>',methods=['GET','POST'])
def group_detail(group_id):
    group=db().execute('SELECT * FROM study_groups WHERE id=?',(group_id,)).fetchone()
    if not group: abort(404)
    if request.method=='POST':
        if current_user()['role']!='admin': abort(403)
        try: sid=int(request.form.get('student_id',''))
        except ValueError: sid=0
        student=db().execute('SELECT id FROM students WHERE id=? AND grade_level=?',(sid,group['grade_level'])).fetchone()
        if not student: flash('Choose a student from the same secondary grade.','error')
        else:
            db().execute('INSERT OR IGNORE INTO enrollments(student_id,group_id) VALUES(?,?)',(sid,group_id));db().commit()
            flash('Student enrolled.','success')
        return redirect(url_for('group_detail',group_id=group_id))
    members=db().execute('''SELECT s.* FROM students s JOIN enrollments e ON e.student_id=s.id
        WHERE e.group_id=? ORDER BY s.name''',(group_id,)).fetchall()
    available=db().execute('''SELECT id,name FROM students WHERE grade_level=? AND id NOT IN
        (SELECT student_id FROM enrollments WHERE group_id=?) ORDER BY name''',(group['grade_level'],group_id)).fetchall()
    return render_template('group_detail.html',active='groups',group=group,members=members,available=available)


@app.post('/groups/<int:group_id>/remove/<int:student_id>')
@admin_required
def remove_group_member(group_id,student_id):
    db().execute('DELETE FROM enrollments WHERE group_id=? AND student_id=?',(group_id,student_id));db().commit()
    return redirect(url_for('group_detail',group_id=group_id))


@app.post('/groups/<int:group_id>/delete')
@admin_required
def delete_group(group_id):
    db().execute('DELETE FROM study_groups WHERE id=?',(group_id,));db().commit()
    flash('Group deleted.','success');return redirect(url_for('groups'))


@app.get('/exams')
def exams():
    rows=db().execute('''SELECT e.*,COUNT(s.student_id) AS graded FROM exams e
        LEFT JOIN exam_scores s ON s.exam_id=e.id GROUP BY e.id ORDER BY e.exam_date DESC,e.id DESC''').fetchall()
    return render_template('exams.html',active='exams',exams_rows=rows)


@app.post('/exams/add')
@admin_required
def add_exam():
    title=request.form.get('title','').strip(); grade=request.form.get('grade_level','')
    day=request.form.get('exam_date',''); raw=request.form.get('max_score','')
    try: max_score=float(raw)
    except ValueError: max_score=0
    if not title or len(title)>120 or grade not in GRADE_LABELS or not 0<max_score<=10000 or valid_day(day)!=day:
        flash('Enter a valid exam title, grade, date and max score.','error')
    else:
        db().execute('INSERT INTO exams(title,grade_level,exam_date,max_score) VALUES(?,?,?,?)',(title,grade,day,max_score))
        db().commit();flash('Exam created.','success')
    return redirect(url_for('exams'))


@app.route('/exams/<int:exam_id>',methods=['GET','POST'])
def exam_detail(exam_id):
    exam=db().execute('SELECT * FROM exams WHERE id=?',(exam_id,)).fetchone()
    if not exam: abort(404)
    students=db().execute('SELECT id,name FROM students WHERE grade_level=? ORDER BY name',(exam['grade_level'],)).fetchall()
    if request.method=='POST':
        if current_user()['role'] != 'admin': abort(403)
        if exam['published_at']:
            flash('Results are published. Reopen them before changing scores.', 'error')
            return redirect(url_for('exam_detail', exam_id=exam_id))
        for student in students:
            raw=request.form.get('score_'+str(student['id']),'').strip()
            if not raw:
                db().execute('DELETE FROM exam_scores WHERE exam_id=? AND student_id=?',(exam_id,student['id']))
                continue
            try: score=float(raw)
            except ValueError:
                db().rollback();flash('Scores must be numbers.','error');return redirect(url_for('exam_detail',exam_id=exam_id))
            if not 0<=score<=exam['max_score']:
                db().rollback();flash('Score cannot exceed the maximum or be negative.','error');return redirect(url_for('exam_detail',exam_id=exam_id))
            db().execute('''INSERT INTO exam_scores(exam_id,student_id,score) VALUES(?,?,?)
                ON CONFLICT(exam_id,student_id) DO UPDATE SET score=excluded.score''',(exam_id,student['id'],score))
        db().commit();flash('Scores saved.','success');return redirect(url_for('exam_detail',exam_id=exam_id))
    scores={r['student_id']:r['score'] for r in db().execute('SELECT student_id,score FROM exam_scores WHERE exam_id=?',(exam_id,))}
    notice_counts=db().execute('SELECT status,COUNT(*) AS total FROM parent_notifications WHERE exam_id=? GROUP BY status', (exam_id,)).fetchall()
    return render_template('exam_detail.html',active='exams',exam=exam,students=students,scores=scores,notice_counts={r['status']:r['total'] for r in notice_counts})




def format_mark(value):
    return f'{value:g}'


def parent_number(raw):
    'Normalize Egypt mobile numbers for wa.me / WhatsApp Cloud API.'
    digits=re.sub(r'\D', '', raw or '')
    if digits.startswith('00'): digits=digits[2:]
    if digits.startswith('0') and len(digits)==11: digits='20'+digits[1:]
    if len(digits)==10 and digits.startswith('1'): digits='20'+digits
    return digits if re.fullmatch(r'201[0125]\d{8}', digits) else None


def grade_message(row):
    return (f"Student Studio\nالسلام عليكم،\nنحيط حضرتكم علمًا بنتيجة الطالب {row['student_name']}.\n"
            f"الامتحان: {row['exam_title']}\nالدرجة: {format_mark(row['score'])}/{format_mark(row['max_score'])}\n"
            "مع تمنياتنا للطالب بدوام النجاح والتوفيق.")


def dispatch_template(row):
    'Submit approved template to Meta. API acceptance does NOT mean delivered.'
    token=os.environ.get('WA_ACCESS_TOKEN','').strip()
    phone_id=os.environ.get('WA_PHONE_NUMBER_ID','').strip()
    template=os.environ.get('WA_TEMPLATE_NAME','').strip()
    lang=os.environ.get('WA_TEMPLATE_LANGUAGE','ar').strip()
    if not all((token,phone_id,template)): return False,'WhatsApp API is not configured.'
    data={"messaging_product":"whatsapp","to":row['phone'],"type":"template",
        "template":{"name":template,"language":{"code":lang},"components":[
            {"type":"body","parameters":[{"type":"text","text":row['student_name']},
            {"type":"text","text":row['exam_title']},
            {"type":"text","text":f"{format_mark(row['score'])}/{format_mark(row['max_score'])}"}]}]}}
    version=os.environ.get('WA_API_VERSION','v23.0')
    url=f'https://graph.facebook.com/{version}/{phone_id}/messages'
    req=urllib.request.Request(url,data=json.dumps(data).encode(),headers={'Authorization':f'Bearer {token}', 'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=8) as response:
            result=json.load(response)
            if result.get('messages'): return True,''
            return False,'WhatsApp did not return a message ID.'
    except (urllib.error.URLError,TimeoutError,ValueError) as e:
        # Do not display network error body (may contain private details).
        return False,f'WhatsApp API request failed ({type(e).__name__}).'


def queued_notifications(exam_id):
    return db().execute('''SELECT n.*,s.name AS student_name,e.title AS exam_title,e.max_score
       FROM parent_notifications n JOIN students s ON s.id=n.student_id
       JOIN exams e ON e.id=n.exam_id WHERE n.exam_id=? ORDER BY s.name''',(exam_id,)).fetchall()


@app.post('/exams/<int:exam_id>/publish')
@admin_required
def publish_results(exam_id):
    exam=db().execute('SELECT * FROM exams WHERE id=?',(exam_id,)).fetchone()
    if not exam: abort(404)
    if exam['published_at']:
        flash('Results were already published. No messages were duplicated.','error')
        return redirect(url_for('exam_detail',exam_id=exam_id))
    if request.form.get('parent_consent') != 'yes':
        flash('Confirm that parents consented to receiving exam results.','error')
        return redirect(url_for('exam_detail',exam_id=exam_id))
    rows=db().execute('''SELECT ss.student_id,ss.score,s.parent_phone FROM exam_scores ss
        JOIN students s ON s.id=ss.student_id WHERE ss.exam_id=?''',(exam_id,)).fetchall()
    if not rows:
        flash('Enter and save grades before publishing.','error')
        return redirect(url_for('exam_detail',exam_id=exam_id))
    for row in rows:
        phone=parent_number(row['parent_phone']) or ''
        existing=db().execute('SELECT score,phone,status FROM parent_notifications WHERE exam_id=? AND student_id=?',(exam_id,row['student_id'])).fetchone()
        if existing and existing['score']==row['score'] and existing['phone']==phone:
            continue  # no accidental resend on an unchanged republish
        db().execute('''INSERT INTO parent_notifications(exam_id,student_id,score,phone,status,last_error)
          VALUES(?,?,?,?,?,?) ON CONFLICT(exam_id,student_id) DO UPDATE SET
          score=excluded.score,phone=excluded.phone,status=excluded.status,
          last_error=excluded.last_error,updated_at=CURRENT_TIMESTAMP''',
          (exam_id,row['student_id'],row['score'],phone,'pending' if phone else 'missing_phone','' if phone else 'Invalid or missing Egyptian parent mobile number'))
    db().execute('UPDATE exams SET published_at=CURRENT_TIMESTAMP WHERE id=?',(exam_id,))
    db().commit()
    mode=request.form.get('send_mode','manual')
    if mode=='api':
        if not all(os.environ.get(x) for x in ('WA_ACCESS_TOKEN','WA_PHONE_NUMBER_ID','WA_TEMPLATE_NAME')):
            flash('Results published. WhatsApp API is not configured, so messages remain pending.','error')
        else:
            for row in queued_notifications(exam_id):
                if row['status'] not in ('pending','failed'): continue
                ok,error=dispatch_template(row)
                db().execute('UPDATE parent_notifications SET status=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                           ('submitted' if ok else 'failed',error,row['id']))
            db().commit()
            flash('Results published. API submission attempts recorded; check Notifications.', 'success')
    else:
        flash('Results published. Open Notifications to send the prepared WhatsApp messages manually.', 'success')
    return redirect(url_for('notifications',exam_id=exam_id))


@app.post('/exams/<int:exam_id>/reopen')
@admin_required
def reopen_results(exam_id):
    db().execute('UPDATE exams SET published_at=NULL WHERE id=?',(exam_id,))
    db().commit()
    flash('Results reopened. Already-submitted notifications are not recalled.','success')
    return redirect(url_for('exam_detail',exam_id=exam_id))


@app.get('/notifications')
@admin_required
def notifications():
    exam_id=request.args.get('exam_id',type=int)
    sql='''SELECT n.*,s.name AS student_name,e.title AS exam_title,e.max_score FROM parent_notifications n
       JOIN students s ON s.id=n.student_id JOIN exams e ON e.id=n.exam_id'''
    params=[]
    if exam_id: sql+=' WHERE n.exam_id=?';params.append(exam_id)
    sql+=' ORDER BY n.updated_at DESC,n.id DESC LIMIT 300'
    entries=db().execute(sql,params).fetchall()
    counts={r['status']:r['total'] for r in db().execute('SELECT status,COUNT(*) total FROM parent_notifications GROUP BY status')}
    return render_template('notifications.html',active='notifications',entries=entries,counts=counts,api_ready=all(os.environ.get(x) for x in ('WA_ACCESS_TOKEN','WA_PHONE_NUMBER_ID','WA_TEMPLATE_NAME')))


@app.get('/notifications/<int:notification_id>/whatsapp')
@admin_required
def manual_whatsapp(notification_id):
    row=db().execute('''SELECT n.*,s.name AS student_name,e.title AS exam_title,e.max_score
        FROM parent_notifications n JOIN students s ON s.id=n.student_id JOIN exams e ON e.id=n.exam_id WHERE n.id=?''',(notification_id,)).fetchone()
    if not row: abort(404)
    if row['status']=='missing_phone':
        flash('Fix the parent number first.','error');return redirect(url_for('notifications'))
    if row['status']=='submitted':
        flash('Already submitted to WhatsApp API; do not resend.','error');return redirect(url_for('notifications'))
    # Opening the chat cannot verify that the operator pressed Send.
    return redirect('https://wa.me/'+row['phone']+'?text='+quote(grade_message(row)),code=302)


@app.post('/notifications/<int:notification_id>/manual-confirm')
@admin_required
def manual_confirm(notification_id):
    row=db().execute('SELECT status FROM parent_notifications WHERE id=?',(notification_id,)).fetchone()
    if not row: abort(404)
    if row['status'] not in ('submitted','missing_phone'):
        db().execute("UPDATE parent_notifications SET status='manually_confirmed',last_error='',updated_at=CURRENT_TIMESTAMP WHERE id=?",(notification_id,))
        db().commit()
    flash('Marked as manually sent by staff (not independently verified).','success')
    return redirect(url_for('notifications'))


@app.post('/notifications/<int:notification_id>/api-send')
@admin_required
def notification_api_send(notification_id):
    row=db().execute('''SELECT n.*,s.name AS student_name,e.title AS exam_title,e.max_score FROM parent_notifications n
        JOIN students s ON s.id=n.student_id JOIN exams e ON e.id=n.exam_id WHERE n.id=?''',(notification_id,)).fetchone()
    if not row: abort(404)
    if row['status'] not in ('pending','failed'):
        flash('This result has already been processed, or the phone number is missing.','error')
    else:
        ok,error=dispatch_template(row)
        db().execute('UPDATE parent_notifications SET status=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',('submitted' if ok else 'failed',error,notification_id))
        db().commit()
        flash('Submitted to WhatsApp API (delivery unconfirmed).' if ok else error,'success' if ok else 'error')
    return redirect(url_for('notifications'))

@app.post('/exams/<int:exam_id>/delete')
@admin_required
def delete_exam(exam_id):
    db().execute('DELETE FROM exams WHERE id=?',(exam_id,));db().commit();return redirect(url_for('exams'))


@app.get('/payments')
@admin_required
def payments():
    month=request.args.get('month',date.today().strftime('%Y-%m'))
    if len(month)!=7 or month[4]!='-':month=date.today().strftime('%Y-%m')
    rows=db().execute('''SELECT p.*,s.name AS student_name,s.grade_level FROM payments p
        JOIN students s ON s.id=p.student_id WHERE p.month=? ORDER BY p.paid_on DESC,p.id DESC''',(month,)).fetchall()
    students=db().execute('SELECT id,name,grade_level FROM students ORDER BY name').fetchall()
    total=sum(row['amount'] for row in rows)
    return render_template('payments.html',active='payments',payments_rows=rows,students=students,total=total,month=month)


@app.post('/payments/add')
@admin_required
def add_payment():
    try: sid=int(request.form.get('student_id','')); amount=float(request.form.get('amount',''))
    except ValueError: sid=0;amount=0
    month=request.form.get('month','');day=request.form.get('paid_on','');notes=request.form.get('notes','').strip()
    valid_student=db().execute('SELECT id FROM students WHERE id=?',(sid,)).fetchone()
    if not valid_student or not 0<amount<10000000 or len(month)!=7 or month[4]!='-' or valid_day(day)!=day or len(notes)>300:
        flash('Check student, amount, payment date and month.','error')
    else:
        db().execute('INSERT INTO payments(student_id,amount,month,paid_on,notes) VALUES(?,?,?,?,?)',(sid,amount,month,day,notes))
        db().commit();flash('Payment saved.','success')
    return redirect(url_for('payments',month=month))


@app.post('/payments/<int:payment_id>/delete')
@admin_required
def delete_payment(payment_id):
    db().execute('DELETE FROM payments WHERE id=?',(payment_id,));db().commit();return redirect(url_for('payments'))


@app.get('/reports')
@admin_required
def reports():
    rows=db().execute('''SELECT s.id,s.name,s.grade_level,
        COUNT(DISTINCT CASE WHEN a.status='present' THEN a.id END) AS present,
        COUNT(DISTINCT CASE WHEN a.status='absent' THEN a.id END) AS absent,
        COUNT(DISTINCT CASE WHEN a.status='late' THEN a.id END) AS late,
        COUNT(DISTINCT es.exam_id) AS exams_count,ROUND(AVG(es.score / ex.max_score * 100),1) AS avg_pct,
        COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.student_id=s.id),0) AS paid
        FROM students s LEFT JOIN attendance a ON a.person_type='student' AND a.person_id=s.id
        LEFT JOIN exam_scores es ON es.student_id=s.id LEFT JOIN exams ex ON ex.id=es.exam_id
        GROUP BY s.id ORDER BY s.grade_level,s.name''').fetchall()
    return render_template('reports.html',active='reports',rows=rows)


@app.get('/reports/export')
@admin_required
def export_report():
    rows=db().execute('''SELECT s.name,s.grade_level,
        (SELECT COUNT(*) FROM attendance a WHERE a.person_type='student' AND a.person_id=s.id AND a.status='present') AS present,
        (SELECT COUNT(*) FROM attendance a WHERE a.person_type='student' AND a.person_id=s.id AND a.status='absent') AS absent,
        (SELECT ROUND(AVG(es.score / e.max_score * 100),1) FROM exam_scores es JOIN exams e ON e.id=es.exam_id WHERE es.student_id=s.id) AS average,
        (SELECT COALESCE(SUM(p.amount),0) FROM payments p WHERE p.student_id=s.id) AS paid
        FROM students s ORDER BY s.name''').fetchall()
    buf=io.StringIO();writer=csv.writer(buf);writer.writerow(['Student','Grade','Present','Absent','Average %','Total paid'])
    for r in rows:writer.writerow([r['name'],GRADE_LABELS.get(r['grade_level'],r['grade_level']),r['present'],r['absent'],r['average'] if r['average'] is not None else '',r['paid']])
    return Response('\ufeff'+buf.getvalue(),mimetype='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename=student_report.csv'})


@app.get('/users')
@admin_required
def users():
    rows=db().execute('SELECT id,username,role FROM users ORDER BY id').fetchall()
    return render_template('users.html',active='users',users=rows)


@app.post('/users/add')
@admin_required
def add_user():
    username=request.form.get('username','').strip();password=request.form.get('password','');role=request.form.get('role','')
    if len(username)<3 or len(username)>40 or len(password)<10 or role not in ('admin','assistant'):
        flash('Use a username (3–40 chars), password (10+ chars), and role.','error')
    else:
        try:
            db().execute('INSERT INTO users(username,password_hash,role) VALUES(?,?,?)',(username,generate_password_hash(password),role))
            db().commit();flash('Account created.','success')
        except sqlite3.IntegrityError: flash('Username is already taken.','error')
    return redirect(url_for('users'))


@app.post('/users/<int:user_id>/delete')
@admin_required
def delete_user(user_id):
    if user_id == current_user()['id']:
        flash('You cannot delete your own account.','error')
    else:
        db().execute('DELETE FROM users WHERE id=?',(user_id,));db().commit();flash('Account deleted.','success')
    return redirect(url_for('users'))



@app.get('/students/<int:student_id>/qr.png')
def student_qr_image(student_id):
    """Generate a private QR card image on demand; require a logged-in staff member."""
    student = db().execute('SELECT qr_token FROM students WHERE id=?', (student_id,)).fetchone()
    if not student: abort(404)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=9, border=3)
    qr.add_data('SSQR1:' + student['qr_token'])
    qr.make(fit=True)
    out = io.BytesIO()
    qr.make_image(fill_color='#0A172A', back_color='#FFFFFF').save(out, format='PNG')
    return Response(out.getvalue(), mimetype='image/png', headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})


@app.get('/qr-attendance')
def qr_attendance():
    day = valid_day(request.args.get('date'))
    count = db().execute("SELECT COUNT(*) FROM attendance WHERE person_type='student' AND attendance_date=? AND status='present'", (day,)).fetchone()[0]
    return render_template('qr_attendance.html', active='qr_attendance', selected_date=day, count=count)


@app.post('/qr-attendance/scan')
def qr_scan():
    """Staff only (existing login gate + CSRF). One present record per student per date."""
    raw = request.form.get('qr_value', '').strip()
    day = valid_day(request.form.get('date'))
    if not raw.startswith('SSQR1:') or len(raw) > 160:
        flash('Invalid Student Studio QR code.', 'error')
        return redirect(url_for('qr_attendance', date=day))
    student = db().execute('SELECT id,name FROM students WHERE qr_token=?', (raw[6:],)).fetchone()
    if student is None:
        flash('This QR code is not linked to an active student.', 'error')
        return redirect(url_for('qr_attendance', date=day))
    existing = db().execute("SELECT status FROM attendance WHERE person_type='student' AND person_id=? AND attendance_date=?", (student['id'], day)).fetchone()
    if existing and existing['status'] == 'present':
        flash(f"{student['name']} is already present for {day}.", 'error')
    else:
        db().execute("""INSERT INTO attendance(person_type,person_id,attendance_date,status)
             VALUES('student',?,?,'present') ON CONFLICT(person_type,person_id,attendance_date)
             DO UPDATE SET status='present'""", (student['id'], day))
        db().commit()
        flash(f"Attendance recorded for {student['name']} ({day}).", 'success')
    return redirect(url_for('qr_attendance', date=day))

init_db()
if __name__ == '__main__':
    # 5050 avoids colliding with an older Flask app still using 5000.
    app.run(debug=os.environ.get('FLASK_DEBUG')=='1', port=int(os.environ.get('PORT', '5050')))
