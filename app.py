import csv
import io
import os
import shutil
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, flash, g, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'database' / 'hotel.db'
BACKUPS = ROOT / 'Backup'
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('HOTEL_SECRET_KEY', 'change-this-local-secret')
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024
app.config['TEMPLATES_AUTO_RELOAD'] = True
HOTEL_TIMEZONE_NAME = os.environ.get('HOTEL_TIMEZONE', 'Asia/Kolkata')
try:
    HOTEL_TIMEZONE = ZoneInfo(HOTEL_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    if HOTEL_TIMEZONE_NAME != 'Asia/Kolkata':
        raise
    # Windows Python may not ship an IANA database; Kolkata is fixed UTC+05:30.
    HOTEL_TIMEZONE = timezone(timedelta(hours=5, minutes=30), name='Asia/Kolkata')

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rooms(id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, type TEXT NOT NULL, floor INTEGER DEFAULT 1, price REAL NOT NULL, description TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'Available');
CREATE TABLE IF NOT EXISTS guests(id INTEGER PRIMARY KEY, name TEXT NOT NULL, mobile TEXT NOT NULL, email TEXT DEFAULT '', address TEXT DEFAULT '', id_proof TEXT DEFAULT '', UNIQUE(mobile));
CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY, guest_id INTEGER NOT NULL REFERENCES guests(id), room_id INTEGER NOT NULL REFERENCES rooms(id), checkin TEXT NOT NULL, checkout TEXT NOT NULL, adults INTEGER DEFAULT 1, children INTEGER DEFAULT 0, total REAL NOT NULL, status TEXT NOT NULL DEFAULT 'Reserved', created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, checked_in TEXT, checked_out TEXT);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL REFERENCES bookings(id), amount REAL NOT NULL, method TEXT NOT NULL, paid_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS checkins(id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL REFERENCES bookings(id), at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS checkouts(id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL REFERENCES bookings(id), at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, final_bill REAL NOT NULL);
CREATE TABLE IF NOT EXISTS inventory(id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, quantity REAL NOT NULL DEFAULT 0, unit TEXT DEFAULT 'pcs', price REAL DEFAULT 0, minimum REAL DEFAULT 0, updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS inventory_transactions(id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES inventory(id), change REAL NOT NULL, note TEXT DEFAULT '', at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS invoices(id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL REFERENCES bookings(id), amount REAL NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS hotel_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_booking_dates ON bookings(room_id,checkin,checkout,status);
CREATE INDEX IF NOT EXISTS idx_payments_booking ON payments(booking_id);
'''

def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db

def init_db():
    DB.parent.mkdir(exist_ok=True)
    BACKUPS.mkdir(exist_ok=True)
    with connect() as db:
        db.executescript(SCHEMA)
        if not db.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            db.execute('INSERT INTO users(username,password) VALUES(?,?)', ('admin', generate_password_hash('admin123')))
        defaults = {'hotel_name':'Harbor House Hotel','address':'','phone':'','email':'','currency':'₹','tax':'0','invoice_footer':'Thank you for staying with us.'}
        for k,v in defaults.items(): db.execute('INSERT OR IGNORE INTO hotel_settings VALUES(?,?)',(k,v))

@app.before_request
def before():
    init_db()
    with connect() as db:
        g.hotel = dict(db.execute('SELECT key,value FROM hotel_settings').fetchall())

def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get('user'): return redirect(url_for('login', next=request.path))
        return fn(*a, **kw)
    return wrapper

@app.context_processor
def inject(): return {'hotel': getattr(g,'hotel',{}), 'today':datetime.now(HOTEL_TIMEZONE).date().isoformat()}

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method=='POST':
        with connect() as db: user=db.execute('SELECT * FROM users WHERE username=?',(request.form.get('username',''),)).fetchone()
        if user and check_password_hash(user['password'],request.form.get('password','')):
            session['user']=user['username']; return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Username or password is incorrect.','error')
    return render_template('login.html')

@app.get('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

@app.get('/')
@login_required
def dashboard():
    with connect() as db:
        local_today=datetime.now(HOTEL_TIMEZONE).date()
        today_text=local_today.isoformat()
        room_status_sql="""CASE
            WHEN r.status='Maintenance' THEN 'Maintenance'
            WHEN r.status='Occupied' THEN 'Occupied'
            WHEN r.status='Reserved' OR EXISTS(
                SELECT 1 FROM bookings b
                WHERE b.room_id=r.id AND b.status='Reserved' AND b.checkout>=?
            ) THEN 'Reserved'
            ELSE 'Available' END"""
        rooms=db.execute(f'SELECT r.*, {room_status_sql} AS display_status FROM rooms r ORDER BY r.number',(today_text,)).fetchall()
        stats={r['status'].lower():r['n'] for r in db.execute(f'SELECT display_status AS status,COUNT(*) AS n FROM (SELECT {room_status_sql} AS display_status FROM rooms r) GROUP BY display_status',(today_text,)).fetchall()}
        stats['total_rooms']=sum(stats.values())
        today_start=datetime.combine(local_today,time.min,HOTEL_TIMEZONE).astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=' ')
        tomorrow_start=datetime.combine(local_today+timedelta(days=1),time.min,HOTEL_TIMEZONE).astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=' ')
        stats['bookings_today']=db.execute("SELECT count(*) FROM bookings WHERE created>=? AND created<? AND status!='Cancelled'",(today_start,tomorrow_start)).fetchone()[0]
        stats['booked_today']=db.execute("SELECT coalesce(sum(total),0) FROM bookings WHERE created>=? AND created<? AND status!='Cancelled'",(today_start,tomorrow_start)).fetchone()[0]
        stats['checkins_today']=db.execute('SELECT count(*) FROM checkins WHERE at>=? AND at<?',(today_start,tomorrow_start)).fetchone()[0]
        stats['checkouts_today']=db.execute('SELECT count(*) FROM checkouts WHERE at>=? AND at<?',(today_start,tomorrow_start)).fetchone()[0]
        stats['revenue_today']=db.execute('SELECT coalesce(sum(amount),0) FROM payments WHERE paid_at>=? AND paid_at<?',(today_start,tomorrow_start)).fetchone()[0]
        stats['pending']=db.execute("SELECT coalesce(sum(b.total-coalesce(p.paid,0)),0) FROM bookings b LEFT JOIN (SELECT booking_id,sum(amount) paid FROM payments GROUP BY booking_id) p ON p.booking_id=b.id WHERE b.status!='Cancelled'").fetchone()[0]
        month_start=datetime.combine(local_today.replace(day=1),time.min,HOTEL_TIMEZONE).astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=' ')
        next_month=(local_today.replace(day=28)+timedelta(days=4)).replace(day=1)
        next_month_start=datetime.combine(next_month,time.min,HOTEL_TIMEZONE).astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=' ')
        stats['monthly_booked']=db.execute("SELECT coalesce(sum(total),0) FROM bookings WHERE created>=? AND created<? AND status!='Cancelled'",(month_start,next_month_start)).fetchone()[0]
        month_start_date=local_today.replace(day=1)
        today_cutoff=local_today+timedelta(days=1)
        booked_ranges=db.execute("SELECT checkin,checkout FROM bookings WHERE status!='Cancelled' AND checkin<? AND checkout>?",(today_cutoff.isoformat(),month_start_date.isoformat())).fetchall()
        booked_room_nights=0
        for booking_range in booked_ranges:
            range_start=max(date.fromisoformat(booking_range['checkin']),month_start_date)
            range_end=min(date.fromisoformat(booking_range['checkout']),today_cutoff)
            booked_room_nights+=max(0,(range_end-range_start).days)
        stats['average_booked_rooms']=booked_room_nights/local_today.day
        stats['monthly_collected']=db.execute('SELECT coalesce(sum(amount),0) FROM payments WHERE paid_at>=? AND paid_at<?',(month_start,next_month_start)).fetchone()[0]
        recent=db.execute('SELECT b.*,g.name guest,r.number room FROM bookings b JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id ORDER BY b.id DESC LIMIT 8').fetchall()
        chart=[]
        for offset in range(6,-1,-1):
            day=local_today-timedelta(days=offset)
            start=datetime.combine(day,time.min,HOTEL_TIMEZONE).astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=' ')
            end=datetime.combine(day+timedelta(days=1),time.min,HOTEL_TIMEZONE).astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=' ')
            booked=db.execute("SELECT coalesce(sum(total),0) FROM bookings WHERE created>=? AND created<? AND status!='Cancelled'",(start,end)).fetchone()[0]
            paid=db.execute('SELECT coalesce(sum(amount),0) FROM payments WHERE paid_at>=? AND paid_at<?',(start,end)).fetchone()[0]
            chart.append({'label':day.strftime('%a'),'booked':float(booked),'paid':float(paid)})
    chart_max=max((max(d['booked'],d['paid']) for d in chart),default=0)
    return render_template('dashboard.html', stats=stats, rooms=rooms, recent=recent, chart=chart, chart_max=chart_max)

@app.route('/rooms',methods=['GET','POST'])
@login_required
def rooms_page():
    with connect() as db:
        if request.method=='POST':
            try:
                vals=(request.form['number'].strip(),request.form['type'],int(request.form.get('floor') or 1),float(request.form['price']),request.form.get('description',''))
                rid=request.form.get('id')
                if rid: db.execute('UPDATE rooms SET number=?,type=?,floor=?,price=?,description=?,status=? WHERE id=?',(*vals,request.form.get('status','Available'),rid))
                else: db.execute('INSERT INTO rooms(number,type,floor,price,description) VALUES(?,?,?,?,?)',vals)
                flash('Room saved.','success')
            except sqlite3.IntegrityError: flash('Room number already exists.','error')
            except (ValueError,KeyError): flash('Enter valid room details.','error')
            return redirect(url_for('rooms_page'))
        local_today=datetime.now(HOTEL_TIMEZONE).date().isoformat()
        allrooms=db.execute("""SELECT r.*,CASE
            WHEN r.status='Maintenance' THEN 'Maintenance'
            WHEN r.status='Occupied' THEN 'Occupied'
            WHEN r.status='Reserved' OR EXISTS(
                SELECT 1 FROM bookings b
                WHERE b.room_id=r.id AND b.status='Reserved' AND b.checkout>=?
            ) THEN 'Reserved'
            ELSE 'Available' END AS display_status
            FROM rooms r ORDER BY r.number""",(local_today,)).fetchall()
    edit=next((r for r in allrooms if str(r['id'])==request.args.get('edit')),None)
    return render_template('rooms.html',rooms=allrooms,edit=edit)

@app.post('/rooms/<int:rid>/delete')
@login_required
def delete_room(rid):
    with connect() as db:
        try: db.execute('DELETE FROM rooms WHERE id=?',(rid,)); flash('Room deleted.','success')
        except sqlite3.IntegrityError: flash('Room has booking history and cannot be deleted.','error')
    return redirect(url_for('rooms_page'))

def available(db, rid, start, end, exclude=None):
    q="SELECT count(*) FROM bookings WHERE room_id=? AND status!='Cancelled' AND checkin<? AND checkout>?"
    args=[rid,end,start]
    if exclude: q+=' AND id!=?'; args.append(exclude)
    return db.execute(q,args).fetchone()[0]==0

@app.route('/bookings',methods=['GET','POST'])
@login_required
def bookings():
    with connect() as db:
        if request.method=='POST':
            try:
                rid=int(request.form['room_id']); start=request.form['checkin']; end=request.form['checkout']; bid=request.form.get('id')
                start_date=date.fromisoformat(start); end_date=date.fromisoformat(end)
                if end_date<=start_date: raise ValueError('Check-out must be after check-in.')
                room=db.execute('SELECT * FROM rooms WHERE id=?',(rid,)).fetchone()
                if not room or room['status']=='Maintenance': raise ValueError('Room is unavailable for booking.')
                if not available(db,rid,start,end,int(bid) if bid else None): raise ValueError('Room already has a booking for those dates.')
                total=(end_date-start_date).days*float(room['price'])
                advance=float(request.form.get('advance') or 0)
                if advance<0 or advance>total: raise ValueError('Advance payment must be between zero and the booking total.')
                mobile=request.form['mobile'].strip()
                db.execute('INSERT INTO guests(name,mobile,email,address,id_proof) VALUES(?,?,?,?,?) ON CONFLICT(mobile) DO UPDATE SET name=excluded.name,email=excluded.email,address=excluded.address,id_proof=excluded.id_proof',(request.form['guest'].strip(),mobile,request.form.get('email',''),request.form.get('address',''),request.form.get('id_proof','')))
                guest=db.execute('SELECT id FROM guests WHERE mobile=?',(mobile,)).fetchone()['id']
                if bid: db.execute('UPDATE bookings SET guest_id=?,room_id=?,checkin=?,checkout=?,adults=?,children=?,total=? WHERE id=?',(guest,rid,start,end,int(request.form.get('adults') or 1),int(request.form.get('children') or 0),total,bid))
                else:
                    db.execute('INSERT INTO bookings(guest_id,room_id,checkin,checkout,adults,children,total) VALUES(?,?,?,?,?,?,?)',(guest,rid,start,end,int(request.form.get('adults') or 1),int(request.form.get('children') or 0),total))
                    new_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                    if advance>0: db.execute('INSERT INTO payments(booking_id,amount,method) VALUES(?,?,?)',(new_id,advance,request.form.get('method','Cash')))
                flash('Booking saved.','success')
            except (ValueError,KeyError) as e: flash(str(e) or 'Enter valid booking details.','error')
            return redirect(url_for('bookings'))
        rooms=db.execute("SELECT * FROM rooms WHERE status!='Maintenance' ORDER BY number").fetchall()
        rows=db.execute('SELECT b.*,g.name guest,g.mobile,g.email,g.address,g.id_proof,r.number room,coalesce((SELECT sum(amount) FROM payments p WHERE p.booking_id=b.id),0) paid FROM bookings b JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id ORDER BY b.id DESC').fetchall()
        now=datetime.now(HOTEL_TIMEZONE)
        rows=[{**dict(row),'can_checkout':now>=datetime.combine(date.fromisoformat(row['checkout']),time(12,0),HOTEL_TIMEZONE)} for row in rows]
        if not rooms:
            flash('No bookable rooms are set up yet. Add a room or change a room from Maintenance in Room Management.', 'error')
    edit=next((r for r in rows if str(r['id'])==request.args.get('edit')),None)
    q=request.args.get('q','').lower()
    if q: rows=[r for r in rows if q in r['guest'].lower() or q in r['mobile'] or q in r['room'] or q in str(r['id'])]
    return render_template('bookings.html',bookings=rows,rooms=rooms,edit=edit)

@app.post('/bookings/<int:bid>/cancel')
@login_required
def cancel_booking(bid):
    with connect() as db: db.execute("UPDATE bookings SET status='Cancelled' WHERE id=? AND status='Reserved'",(bid,))
    flash('Booking cancelled.','success'); return redirect(url_for('bookings'))

@app.get('/guests')
@login_required
def guests():
    q=request.args.get('q','')
    with connect() as db: rows=db.execute('SELECT g.*,count(b.id) stays FROM guests g LEFT JOIN bookings b ON b.guest_id=g.id WHERE g.name LIKE ? OR g.mobile LIKE ? GROUP BY g.id ORDER BY g.name',('%'+q+'%','%'+q+'%')).fetchall()
    return render_template('guests.html',guests=rows,q=q)

@app.post('/bookings/<int:bid>/operation')
@login_required
def booking_operation(bid):
    action=request.form.get('action','')
    with connect() as db:
        booking=db.execute('SELECT * FROM bookings WHERE id=?',(bid,)).fetchone()
        if not booking:
            flash('Booking not found.','error')
        elif action=='checkin' and booking['status']=='Reserved':
            db.execute("UPDATE bookings SET status='Occupied',checked_in=CURRENT_TIMESTAMP WHERE id=?",(bid,))
            db.execute("UPDATE rooms SET status='Occupied' WHERE id=?",(booking['room_id'],))
            db.execute('INSERT INTO checkins(booking_id) VALUES(?)',(bid,))
            flash('Guest checked in.','success')
        elif action=='checkout' and booking['status']=='Occupied':
            checkout_at=datetime.combine(date.fromisoformat(booking['checkout']),time(12,0),HOTEL_TIMEZONE)
            if datetime.now(HOTEL_TIMEZONE)<checkout_at:
                flash(f"Checkout is available from 12:00 PM on {booking['checkout']} (hotel local time).",'error')
            else:
                db.execute("UPDATE bookings SET status='Checked out',checked_out=CURRENT_TIMESTAMP WHERE id=?",(bid,))
                db.execute("UPDATE rooms SET status='Available' WHERE id=?",(booking['room_id'],))
                db.execute('INSERT INTO checkouts(booking_id,final_bill) VALUES(?,?)',(bid,booking['total']))
                db.execute('INSERT INTO invoices(booking_id,amount) VALUES(?,?)',(bid,booking['total']))
                flash('Guest checked out. Invoice is available.','success')
        else:
            flash('This booking cannot use that operation.','error')
    return redirect(url_for('bookings'))

@app.route('/payments',methods=['GET','POST'])
@login_required
def payments():
    with connect() as db:
        if request.method=='POST':
            try:
                bid=int(request.form['booking_id']); amount=float(request.form['amount'])
                paid=db.execute('SELECT coalesce(sum(amount),0) FROM payments WHERE booking_id=?',(bid,)).fetchone()[0]
                b=db.execute('SELECT total,status FROM bookings WHERE id=?',(bid,)).fetchone()
                if not b or b['status']=='Cancelled' or amount<=0 or amount>b['total']-paid: raise ValueError('Payment must be positive and cannot exceed the remaining balance.')
                db.execute('INSERT INTO payments(booking_id,amount,method,note) VALUES(?,?,?,?)',(bid,amount,request.form['method'],request.form.get('note',''))); flash('Payment recorded.','success')
            except (ValueError,KeyError) as e: flash(str(e) or 'Invalid payment.','error')
            return redirect(url_for('payments'))
        history=db.execute('SELECT p.*,g.name guest,r.number room FROM payments p JOIN bookings b ON b.id=p.booking_id JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id ORDER BY p.id DESC').fetchall()
        owing=db.execute("SELECT b.id,b.total,g.name guest,r.number room,b.total-coalesce((SELECT sum(amount) FROM payments WHERE booking_id=b.id),0) due FROM bookings b JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id WHERE b.status!='Cancelled' AND b.total>coalesce((SELECT sum(amount) FROM payments WHERE booking_id=b.id),0) ORDER BY b.id DESC").fetchall()
    return render_template('payments.html',history=history,owing=owing)

@app.route('/inventory',methods=['GET','POST'])
@login_required
def inventory():
    with connect() as db:
        if request.method=='POST':
            try:
                name=request.form['name'].strip(); qty=float(request.form['quantity']); minimum=float(request.form.get('minimum') or 0)
                iid=request.form.get('id')
                if iid:
                    db.execute('UPDATE inventory SET name=?,category=?,unit=?,price=?,minimum=? WHERE id=?',(name,request.form['category'],request.form.get('unit','pcs'),float(request.form.get('price') or 0),minimum,iid)); item=int(iid)
                else:
                    db.execute('INSERT INTO inventory(name,category,quantity,unit,price,minimum) VALUES(?,?,?,?,?,?)',(name,request.form['category'],0,request.form.get('unit','pcs'),float(request.form.get('price') or 0),minimum)); item=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                if qty: db.execute('UPDATE inventory SET quantity=quantity+?,updated=CURRENT_TIMESTAMP WHERE id=?',(qty,item)); db.execute('INSERT INTO inventory_transactions(item_id,change,note) VALUES(?,?,?)',(item,qty,request.form.get('note','Stock update')))
                flash('Inventory updated.','success')
            except (ValueError,KeyError): flash('Enter valid inventory details.','error')
            return redirect(url_for('inventory'))
        rows=db.execute('SELECT * FROM inventory ORDER BY name').fetchall()
    edit=next((r for r in rows if str(r['id'])==request.args.get('edit')),None)
    return render_template('inventory.html',items=rows,edit=edit)

@app.post('/inventory/<int:iid>/delete')
@login_required
def delete_inventory(iid):
    with connect() as db:
        try: db.execute('DELETE FROM inventory WHERE id=?',(iid,)); flash('Item deleted.','success')
        except sqlite3.IntegrityError: flash('Item has transaction history; keep it for audit.','error')
    return redirect(url_for('inventory'))

@app.route('/reports')
@login_required
def reports():
    start=request.args.get('start',date.today().replace(day=1).isoformat()); end=request.args.get('end',date.today().isoformat())
    with connect() as db:
        rows=db.execute("SELECT b.id,g.name guest,r.number room,b.checkin,b.checkout,b.total,b.status,coalesce((SELECT sum(amount) FROM payments WHERE booking_id=b.id),0) paid FROM bookings b JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id WHERE date(b.created) BETWEEN date(?) AND date(?) ORDER BY b.id DESC",(start,end)).fetchall()
    return render_template('reports.html',rows=rows,start=start,end=end)

@app.get('/reports.csv')
@login_required
def report_csv():
    start=request.args.get('start',date.today().replace(day=1).isoformat()); end=request.args.get('end',date.today().isoformat())
    with connect() as db: rows=db.execute('SELECT b.id,g.name,r.number,b.checkin,b.checkout,b.total,b.status FROM bookings b JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id WHERE date(b.created) BETWEEN date(?) AND date(?)',(start,end)).fetchall()
    output=io.StringIO(); w=csv.writer(output); w.writerow(['Booking','Guest','Room','Check-in','Check-out','Total','Status']); w.writerows([tuple(r) for r in rows])
    return send_file(io.BytesIO(output.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name='hotel-report.csv')

@app.route('/settings',methods=['GET','POST'])
@login_required
def settings():
    with connect() as db:
        if request.method=='POST':
            for k in ['hotel_name','address','phone','email','currency','tax','invoice_footer']: db.execute('INSERT INTO hotel_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,request.form.get(k,'')))
            new=request.form.get('new_password','')
            if new:
                if len(new)<8: flash('New password must contain at least 8 characters.','error'); return redirect(url_for('settings'))
                db.execute('UPDATE users SET password=? WHERE username=?',(generate_password_hash(new),session['user']))
            flash('Settings saved.','success'); return redirect(url_for('settings'))
        values=dict(db.execute('SELECT key,value FROM hotel_settings').fetchall())
    return render_template('settings.html',values=values,backups=sorted((f.name for f in BACKUPS.glob('*.db')),reverse=True))

@app.get('/backup')
@login_required
def backup():
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S'); target=BACKUPS/f'hotel-{stamp}.db'
    with connect() as db:
        dest=sqlite3.connect(target); db.backup(dest); dest.close()
    flash(f'Backup created: {target.name}','success'); return redirect(url_for('settings'))

@app.post('/backup/restore')
@login_required
def restore_backup():
    upload=request.files.get('backup_file')
    if not upload or not upload.filename:
        flash('Choose a database backup file.','error'); return redirect(url_for('settings'))
    candidate=BACKUPS/'restore-candidate.db'
    try:
        upload.save(candidate)
        check=sqlite3.connect(candidate)
        valid=check.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        tables={r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        check.close()
        required={'users','rooms','guests','bookings','payments'}
        if not valid or not required.issubset(tables):
            candidate.unlink(missing_ok=True); flash('This file is not a valid hotel database backup.','error'); return redirect(url_for('settings'))
        # Preserve the current database before replacing it.
        stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
        current=sqlite3.connect(DB); safety=sqlite3.connect(BACKUPS/f'pre-restore-{stamp}.db'); current.backup(safety); safety.close(); current.close()
        os.replace(candidate,DB)
        flash('Database restored. A pre-restore snapshot was saved.','success')
    except (sqlite3.Error,OSError) as exc:
        candidate.unlink(missing_ok=True); flash(f'Restore failed: {exc}','error')
    return redirect(url_for('settings'))

@app.get('/invoice/<int:bid>')
@login_required
def invoice(bid):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    with connect() as db:
        row=db.execute('SELECT b.*,g.name guest,g.mobile,g.email,r.number room FROM bookings b JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id WHERE b.id=?',(bid,)).fetchone()
        payments=db.execute('SELECT coalesce(sum(amount),0) FROM payments WHERE booking_id=?',(bid,)).fetchone()[0]
        settings=dict(db.execute('SELECT key,value FROM hotel_settings').fetchall())
    if not row: return 'Invoice not found',404
    buf=io.BytesIO(); c=canvas.Canvas(buf,pagesize=A4); width,height=A4
    c.setFillColorRGB(.05,.13,.24); c.rect(0,height-120,width,120,fill=1,stroke=0); c.setFillColorRGB(1,1,1); c.setFont('Helvetica-Bold',22); c.drawString(48,height-58,settings.get('hotel_name','Hotel')); c.setFont('Helvetica',10); c.drawString(48,height-80,settings.get('address','')); c.drawRightString(width-48,height-58,f'INVOICE #{bid:05d}')
    c.setFillColorRGB(.1,.15,.2); y=height-170; c.setFont('Helvetica-Bold',12); c.drawString(48,y,'GUEST'); c.drawString(330,y,'STAY DETAILS'); c.setFont('Helvetica',11); y-=22; c.drawString(48,y,row['guest']); c.drawString(330,y,f"Room {row['room']} · {row['checkin']} to {row['checkout']}"); y-=20; c.drawString(48,y,row['mobile']); c.drawString(330,y,f"{(date.fromisoformat(row['checkout'])-date.fromisoformat(row['checkin'])).days} nights")
    y-=65; c.setFont('Helvetica-Bold',11); c.drawString(48,y,'Description'); c.drawRightString(width-48,y,'Amount'); y-=12; c.line(48,y,width-48,y); y-=26; c.setFont('Helvetica',11); c.drawString(48,y,'Room charges'); c.drawRightString(width-48,y,f"{settings.get('currency','₹')} {row['total']:,.2f}"); y-=30; c.drawString(48,y,'Payments received'); c.drawRightString(width-48,y,f"{settings.get('currency','₹')} {payments:,.2f}"); y-=36; c.setFont('Helvetica-Bold',14); c.drawString(48,y,'Balance due'); c.drawRightString(width-48,y,f"{settings.get('currency','₹')} {max(0,row['total']-payments):,.2f}"); c.setFont('Helvetica',9); c.drawCentredString(width/2,48,settings.get('invoice_footer','Thank you.')); c.save(); buf.seek(0)
    return send_file(buf,mimetype='application/pdf',as_attachment=True,download_name=f'invoice-{bid:05d}.pdf')

@app.errorhandler(413)
def too_large(_): return 'Upload too large.',413

if __name__=='__main__':
    init_db()
    # Automatic startup snapshot (one per calendar day).
    if DB.exists() and not any(BACKUPS.glob(f'auto-{date.today().isoformat()}*.db')):
        with connect() as db:
            dst=sqlite3.connect(BACKUPS/f'auto-{date.today().isoformat()}.db'); db.backup(dst); dst.close()
    app.run(host='127.0.0.1',port=5000,debug=False)
