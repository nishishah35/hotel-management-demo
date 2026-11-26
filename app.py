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
CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY, guest_id INTEGER NOT NULL REFERENCES guests(id), room_id INTEGER NOT NULL REFERENCES rooms(id), checkin TEXT NOT NULL, checkout TEXT NOT NULL, adults INTEGER DEFAULT 1, children INTEGER DEFAULT 0, total REAL NOT NULL, discount REAL NOT NULL DEFAULT 0, tax_rate REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Reserved', created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, checked_in TEXT, checked_out TEXT);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL REFERENCES bookings(id), amount REAL NOT NULL, method TEXT NOT NULL, paid_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS checkins(id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL REFERENCES bookings(id), at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS checkouts(id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL REFERENCES bookings(id), at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, final_bill REAL NOT NULL);
CREATE TABLE IF NOT EXISTS inventory(id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, quantity REAL NOT NULL DEFAULT 0, unit TEXT DEFAULT 'pcs', price REAL DEFAULT 0, minimum REAL DEFAULT 0, updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS inventory_transactions(id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES inventory(id), change REAL NOT NULL, note TEXT DEFAULT '', at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS booking_charges(id INTEGER PRIMARY KEY, booking_id INTEGER NOT NULL REFERENCES bookings(id), item_id INTEGER REFERENCES inventory(id) ON DELETE SET NULL, item_name TEXT NOT NULL, quantity REAL NOT NULL, unit_price REAL NOT NULL, amount REAL NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_booking_charges_booking ON booking_charges(booking_id);
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
        booking_columns={row['name'] for row in db.execute('PRAGMA table_info(bookings)')}
        if 'discount' not in booking_columns: db.execute('ALTER TABLE bookings ADD COLUMN discount REAL NOT NULL DEFAULT 0')
        if 'tax_rate' not in booking_columns: db.execute('ALTER TABLE bookings ADD COLUMN tax_rate REAL NOT NULL DEFAULT 0')
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
        stats['booked_today']=db.execute("SELECT coalesce(sum(round(total+total*tax_rate/100,2)),0) FROM bookings WHERE created>=? AND created<? AND status!='Cancelled'",(today_start,tomorrow_start)).fetchone()[0]
        stats['booked_today']+=db.execute('SELECT coalesce(sum(round(c.amount+c.amount*b.tax_rate/100,2)),0) FROM booking_charges c JOIN bookings b ON b.id=c.booking_id WHERE c.created>=? AND c.created<?',(today_start,tomorrow_start)).fetchone()[0]
        stats['checkins_today']=db.execute('SELECT count(*) FROM checkins WHERE at>=? AND at<?',(today_start,tomorrow_start)).fetchone()[0]
        stats['checkouts_today']=db.execute('SELECT count(*) FROM checkouts WHERE at>=? AND at<?',(today_start,tomorrow_start)).fetchone()[0]
        stats['revenue_today']=db.execute('SELECT coalesce(sum(amount),0) FROM payments WHERE paid_at>=? AND paid_at<?',(today_start,tomorrow_start)).fetchone()[0]
        stats['pending']=db.execute("SELECT coalesce(sum(round(b.total+coalesce(c.charges,0)+(b.total+coalesce(c.charges,0))*b.tax_rate/100,2)-coalesce(p.paid,0)),0) FROM bookings b LEFT JOIN (SELECT booking_id,sum(amount) paid FROM payments GROUP BY booking_id) p ON p.booking_id=b.id LEFT JOIN (SELECT booking_id,sum(amount) charges FROM booking_charges GROUP BY booking_id) c ON c.booking_id=b.id WHERE b.status!='Cancelled'").fetchone()[0]
        month_start=datetime.combine(local_today.replace(day=1),time.min,HOTEL_TIMEZONE).astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=' ')
        next_month=(local_today.replace(day=28)+timedelta(days=4)).replace(day=1)
        next_month_start=datetime.combine(next_month,time.min,HOTEL_TIMEZONE).astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=' ')
        stats['monthly_booked']=db.execute("SELECT coalesce(sum(round(total+total*tax_rate/100,2)),0) FROM bookings WHERE created>=? AND created<? AND status!='Cancelled'",(month_start,next_month_start)).fetchone()[0]
        stats['monthly_booked']+=db.execute('SELECT coalesce(sum(round(c.amount+c.amount*b.tax_rate/100,2)),0) FROM booking_charges c JOIN bookings b ON b.id=c.booking_id WHERE c.created>=? AND c.created<?',(month_start,next_month_start)).fetchone()[0]
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
        recent=db.execute('SELECT b.*,g.name guest,r.number room,coalesce(c.charges,0) extras,round((b.total+coalesce(c.charges,0))*(1+b.tax_rate/100),2) bill_total FROM bookings b JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id LEFT JOIN (SELECT booking_id,sum(amount) charges FROM booking_charges GROUP BY booking_id) c ON c.booking_id=b.id ORDER BY b.id DESC LIMIT 8').fetchall()
        chart=[]
        for offset in range(6,-1,-1):
            day=local_today-timedelta(days=offset)
            start=datetime.combine(day,time.min,HOTEL_TIMEZONE).astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=' ')
            end=datetime.combine(day+timedelta(days=1),time.min,HOTEL_TIMEZONE).astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=' ')
            booked=db.execute("SELECT coalesce(sum(round(total+total*tax_rate/100,2)),0) FROM bookings WHERE created>=? AND created<? AND status!='Cancelled'",(start,end)).fetchone()[0]
            booked+=db.execute('SELECT coalesce(sum(round(c.amount+c.amount*b.tax_rate/100,2)),0) FROM booking_charges c JOIN bookings b ON b.id=c.booking_id WHERE c.created>=? AND c.created<?',(start,end)).fetchone()[0]
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
                room_subtotal=round((end_date-start_date).days*float(room['price']),2)
                advance=float(request.form.get('advance') or 0)
                mobile=request.form['mobile'].strip()
                db.execute('INSERT INTO guests(name,mobile,email,address,id_proof) VALUES(?,?,?,?,?) ON CONFLICT(mobile) DO UPDATE SET name=excluded.name,email=excluded.email,address=excluded.address,id_proof=excluded.id_proof',(request.form['guest'].strip(),mobile,request.form.get('email',''),request.form.get('address',''),request.form.get('id_proof','')))
                guest=db.execute('SELECT id FROM guests WHERE mobile=?',(mobile,)).fetchone()['id']
                previous_sql="SELECT 1 FROM bookings WHERE guest_id=? AND status!='Cancelled'"
                previous_params=[guest]
                if bid: previous_sql+=' AND id!=?'; previous_params.append(int(bid))
                is_returning=bool(db.execute(previous_sql+' LIMIT 1',previous_params).fetchone())
                discount=round(room_subtotal*.05,2) if is_returning else 0
                total=round(room_subtotal-discount,2)
                tax_row=db.execute("SELECT value FROM hotel_settings WHERE key='tax'").fetchone()
                tax_rate=max(0,float(tax_row['value'] if tax_row else 0))
                advance_total=round(total+total*tax_rate/100,2)
                if advance<0 or advance>advance_total: raise ValueError('Advance payment cannot exceed the discounted booking total including tax.')
                if bid: db.execute('UPDATE bookings SET guest_id=?,room_id=?,checkin=?,checkout=?,adults=?,children=?,total=?,discount=?,tax_rate=? WHERE id=?',(guest,rid,start,end,int(request.form.get('adults') or 1),int(request.form.get('children') or 0),total,discount,tax_rate,bid))
                else:
                    db.execute('INSERT INTO bookings(guest_id,room_id,checkin,checkout,adults,children,total,discount,tax_rate) VALUES(?,?,?,?,?,?,?,?,?)',(guest,rid,start,end,int(request.form.get('adults') or 1),int(request.form.get('children') or 0),total,discount,tax_rate))
                    new_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
                    if advance>0: db.execute('INSERT INTO payments(booking_id,amount,method) VALUES(?,?,?)',(new_id,advance,request.form.get('method','Cash')))
                flash('Booking saved. A 5% repeat guest discount was applied to room charges.' if discount else 'Booking saved.','success')
            except (ValueError,KeyError) as e: flash(str(e) or 'Enter valid booking details.','error')
            return redirect(url_for('bookings'))
        rooms=db.execute("SELECT * FROM rooms WHERE status!='Maintenance' ORDER BY number").fetchall()
        rows=db.execute('SELECT b.*,g.name guest,g.mobile,g.email,g.address,g.id_proof,r.number room,coalesce((SELECT sum(amount) FROM payments p WHERE p.booking_id=b.id),0) paid,coalesce((SELECT sum(amount) FROM booking_charges c WHERE c.booking_id=b.id),0) extras,round((b.total+coalesce((SELECT sum(amount) FROM booking_charges c WHERE c.booking_id=b.id),0))*(1+b.tax_rate/100),2) bill_total FROM bookings b JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id ORDER BY b.id DESC').fetchall()
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
                charges=db.execute('SELECT coalesce(sum(amount),0) FROM booking_charges WHERE booking_id=?',(bid,)).fetchone()[0]
                tax_rate=float(booking['tax_rate'] or 0)
                final_bill=round((booking['total']+charges)*(1+tax_rate/100),2)
                db.execute('INSERT INTO checkouts(booking_id,final_bill) VALUES(?,?)',(bid,final_bill))
                db.execute('INSERT INTO invoices(booking_id,amount) VALUES(?,?)',(bid,final_bill))
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
                b=db.execute('SELECT total,status,tax_rate FROM bookings WHERE id=?',(bid,)).fetchone()
                extras=db.execute('SELECT coalesce(sum(amount),0) FROM booking_charges WHERE booking_id=?',(bid,)).fetchone()[0]
                if not b or b['status']=='Cancelled' or amount<=0 or amount>round((b['total']+extras)*(1+float(b['tax_rate'] or 0)/100),2)-paid: raise ValueError('Payment must be positive and cannot exceed the remaining balance.')
                db.execute('INSERT INTO payments(booking_id,amount,method,note) VALUES(?,?,?,?)',(bid,amount,request.form['method'],request.form.get('note',''))); flash('Payment recorded.','success')
            except (ValueError,KeyError) as e: flash(str(e) or 'Invalid payment.','error')
            return redirect(url_for('payments'))
        history=db.execute('SELECT p.*,g.name guest,r.number room FROM payments p JOIN bookings b ON b.id=p.booking_id JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id ORDER BY p.id DESC').fetchall()
        owing=db.execute("SELECT b.id,round((b.total+coalesce(c.charges,0))*(1+b.tax_rate/100),2) total,g.name guest,r.number room,round((b.total+coalesce(c.charges,0))*(1+b.tax_rate/100),2)-coalesce(p.paid,0) due FROM bookings b JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id LEFT JOIN (SELECT booking_id,sum(amount) charges FROM booking_charges GROUP BY booking_id) c ON c.booking_id=b.id LEFT JOIN (SELECT booking_id,sum(amount) paid FROM payments GROUP BY booking_id) p ON p.booking_id=b.id WHERE b.status!='Cancelled' AND round((b.total+coalesce(c.charges,0))*(1+b.tax_rate/100),2)>coalesce(p.paid,0) ORDER BY b.id DESC").fetchall()
    return render_template('payments.html',history=history,owing=owing)

@app.post('/billing/hotel-details')
@login_required
def update_billing_hotel_details():
    with connect() as db:
        for key in ['address','phone','email','gstin','pan']:
            db.execute('INSERT INTO hotel_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,request.form.get(key,'').strip()))
    flash('Hotel bill details saved.','success')
    return redirect(url_for('billing'))

@app.get('/billing')
@login_required
def billing():
    edit_charge_id=request.args.get('edit_charge','')
    with connect() as db:
        active_bookings=db.execute("SELECT b.id,g.name guest,r.number room,b.checkin,b.checkout FROM bookings b JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id WHERE b.status='Occupied' ORDER BY b.id DESC").fetchall()
        editing_charge=None
        if edit_charge_id.isdigit():
            editing_charge=db.execute("SELECT c.*,b.status booking_status FROM booking_charges c JOIN bookings b ON b.id=c.booking_id WHERE c.id=? AND b.status='Occupied'",(int(edit_charge_id),)).fetchone()
        if editing_charge:
            items=db.execute('SELECT * FROM inventory WHERE quantity>0 OR id=? ORDER BY name',(editing_charge['item_id'],)).fetchall()
        else:
            items=db.execute('SELECT * FROM inventory WHERE quantity>0 ORDER BY name').fetchall()
        rows=db.execute("SELECT b.id,b.checkin,b.checkout,b.total,b.discount,b.tax_rate,b.status,b.created,g.name guest,g.mobile,r.number room,coalesce((SELECT sum(amount) FROM payments p WHERE p.booking_id=b.id),0) paid,coalesce((SELECT sum(amount) FROM booking_charges c WHERE c.booking_id=b.id),0) extras,round((b.total+coalesce((SELECT sum(amount) FROM booking_charges c WHERE c.booking_id=b.id),0))*(1+b.tax_rate/100),2) bill_total FROM bookings b JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id WHERE b.status!='Cancelled' ORDER BY b.id DESC").fetchall()
        charge_rows=db.execute('SELECT id,booking_id,item_id,item_name,quantity,unit_price,amount FROM booking_charges ORDER BY id').fetchall()
    charges_by_booking={}
    for charge in charge_rows:
        charges_by_booking.setdefault(charge['booking_id'],[]).append(charge)
    bills=[{**dict(row),'bill_lines':charges_by_booking.get(row['id'],[]),'due':max(0,round(row['bill_total']-row['paid'],2))} for row in rows]
    return render_template('billing.html',active_bookings=active_bookings,items=items,bills=bills,editing_charge=editing_charge)

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

@app.post('/billing/guest-charge')
@login_required
def charge_inventory_to_guest():
    try:
        booking_id=int(request.form['booking_id']); item_id=int(request.form['item_id']); quantity=float(request.form['quantity'])
        if quantity<=0: raise ValueError('Quantity must be greater than zero.')
        with connect() as db:
            booking=db.execute("SELECT id,status FROM bookings WHERE id=?",(booking_id,)).fetchone()
            item=db.execute('SELECT * FROM inventory WHERE id=?',(item_id,)).fetchone()
            if not booking or booking['status']!='Occupied': raise ValueError('Select a guest who is currently checked in.')
            if not item: raise ValueError('Inventory item not found.')
            if quantity>item['quantity']: raise ValueError(f"Only {item['quantity']:g} {item['unit']} of {item['name']} are in stock.")
            unit_price=float(item['price']); amount=round(quantity*unit_price,2)
            updated=db.execute('UPDATE inventory SET quantity=quantity-?,updated=CURRENT_TIMESTAMP WHERE id=? AND quantity>=?',(quantity,item_id,quantity))
            if updated.rowcount!=1: raise ValueError('Stock changed before the charge could be saved. Refresh and try again.')
            db.execute('INSERT INTO inventory_transactions(item_id,change,note) VALUES(?,?,?)',(item_id,-quantity,f'Charged to booking #{booking_id}'))
            db.execute('INSERT INTO booking_charges(booking_id,item_id,item_name,quantity,unit_price,amount) VALUES(?,?,?,?,?,?)',(booking_id,item_id,item['name'],quantity,unit_price,amount))
        flash('Item added to the guest bill and stock updated.','success')
    except (ValueError,KeyError) as exc: flash(str(exc) or 'Enter valid guest charge details.','error')
    return redirect(url_for('billing'))

@app.post('/billing/guest-charge/<int:charge_id>/edit')
@login_required
def edit_guest_charge(charge_id):
    try:
        booking_id=int(request.form['booking_id']); item_id=int(request.form['item_id']); quantity=float(request.form['quantity'])
        if quantity<=0: raise ValueError('Quantity must be greater than zero.')
        with connect() as db:
            old=db.execute('SELECT c.*,b.status booking_status FROM booking_charges c JOIN bookings b ON b.id=c.booking_id WHERE c.id=?',(charge_id,)).fetchone()
            booking=db.execute('SELECT id,status FROM bookings WHERE id=?',(booking_id,)).fetchone()
            item=db.execute('SELECT * FROM inventory WHERE id=?',(item_id,)).fetchone()
            if not old: raise ValueError('Guest bill item not found.')
            if old['booking_status']!='Occupied' or not booking or booking['status']!='Occupied': raise ValueError('Charges can only be edited for a guest who is currently checked in.')
            if not item: raise ValueError('Inventory item not found.')
            old_item_id=old['item_id']; old_quantity=float(old['quantity'])
            if old_item_id==item_id:
                change=old_quantity-quantity
                update=db.execute('UPDATE inventory SET quantity=quantity+?,updated=CURRENT_TIMESTAMP WHERE id=? AND quantity+?>=?',(change,item_id,old_quantity,quantity))
                if update.rowcount!=1: raise ValueError(f"Only {item['quantity']+old_quantity:g} {item['unit']} are available for this bill item.")
                if abs(change)>0.000001:
                    db.execute('INSERT INTO inventory_transactions(item_id,change,note) VALUES(?,?,?)',(item_id,change,f'Edited bill item #{charge_id}'))
            else:
                if old_item_id:
                    db.execute('UPDATE inventory SET quantity=quantity+?,updated=CURRENT_TIMESTAMP WHERE id=?',(old_quantity,old_item_id))
                    db.execute('INSERT INTO inventory_transactions(item_id,change,note) VALUES(?,?,?)',(old_item_id,old_quantity,f'Returned stock from edited bill item #{charge_id}'))
                update=db.execute('UPDATE inventory SET quantity=quantity-?,updated=CURRENT_TIMESTAMP WHERE id=? AND quantity>=?',(quantity,item_id,quantity))
                if update.rowcount!=1: raise ValueError(f"Only {item['quantity']:g} {item['unit']} of {item['name']} are in stock.")
                db.execute('INSERT INTO inventory_transactions(item_id,change,note) VALUES(?,?,?)',(item_id,-quantity,f'Edited bill item #{charge_id}'))
            unit_price=float(item['price']); amount=round(quantity*unit_price,2)
            db.execute('UPDATE booking_charges SET booking_id=?,item_id=?,item_name=?,quantity=?,unit_price=?,amount=? WHERE id=?',(booking_id,item_id,item['name'],quantity,unit_price,amount,charge_id))
        flash('Guest bill item updated and stock adjusted.','success')
    except (ValueError,KeyError) as exc: flash(str(exc) or 'Enter valid guest charge details.','error')
    return redirect(url_for('billing'))

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
        rows=db.execute("SELECT b.id,g.name guest,r.number room,b.checkin,b.checkout,round((b.total+coalesce((SELECT sum(amount) FROM booking_charges WHERE booking_id=b.id),0))*(1+b.tax_rate/100),2) total,b.status,coalesce((SELECT sum(amount) FROM payments WHERE booking_id=b.id),0) paid FROM bookings b JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id WHERE date(b.created) BETWEEN date(?) AND date(?) ORDER BY b.id DESC",(start,end)).fetchall()
    return render_template('reports.html',rows=rows,start=start,end=end)

@app.get('/reports.csv')
@login_required
def report_csv():
    start=request.args.get('start',date.today().replace(day=1).isoformat()); end=request.args.get('end',date.today().isoformat())
    with connect() as db: rows=db.execute('SELECT b.id,g.name,r.number,b.checkin,b.checkout,round((b.total+coalesce((SELECT sum(amount) FROM booking_charges WHERE booking_id=b.id),0))*(1+b.tax_rate/100),2),b.status FROM bookings b JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id WHERE date(b.created) BETWEEN date(?) AND date(?)',(start,end)).fetchall()
    output=io.StringIO(); w=csv.writer(output); w.writerow(['Booking','Guest','Room','Check-in','Check-out','Total','Status']); w.writerows([tuple(r) for r in rows])
    return send_file(io.BytesIO(output.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name='hotel-report.csv')

@app.route('/settings',methods=['GET','POST'])
@login_required
def settings():
    with connect() as db:
        if request.method=='POST':
            for k in ['address','phone','email','gstin','pan','currency','tax','invoice_footer']: db.execute('INSERT INTO hotel_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,request.form.get(k,'')))
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
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    with connect() as db:
        row=db.execute('SELECT b.*,g.name guest,g.mobile,g.email,g.address guest_address,g.id_proof,r.number room,r.type room_type,r.price room_price FROM bookings b JOIN guests g ON g.id=b.guest_id JOIN rooms r ON r.id=b.room_id WHERE b.id=?',(bid,)).fetchone()
        payments=db.execute('SELECT coalesce(sum(amount),0) FROM payments WHERE booking_id=?',(bid,)).fetchone()[0]
        charges=db.execute('SELECT item_name,quantity,unit_price,amount FROM booking_charges WHERE booking_id=? ORDER BY id',(bid,)).fetchall()
        settings=dict(db.execute('SELECT key,value FROM hotel_settings').fetchall())
    if not row: return 'Bill not found',404

    checkin_date=date.fromisoformat(row['checkin'])
    checkout_date=date.fromisoformat(row['checkout'])
    nights=max(1,(checkout_date-checkin_date).days)
    discount=round(float(row['discount'] or 0),2)
    room_subtotal=round(float(row['total'])+discount,2)
    extras_total=round(sum(float(charge['amount']) for charge in charges),2)
    subtotal=round(room_subtotal+extras_total,2)
    taxable=round(subtotal-discount,2)
    tax_rate=float(row['tax_rate'] or 0)
    tax=round(taxable*tax_rate/100,2)
    raw_total=taxable+tax
    bill_total=round(raw_total,2)
    roundoff=round(bill_total-raw_total,2)
    room_rate=room_subtotal/nights
    invoice_date=datetime.now(HOTEL_TIMEZONE).date()
    fy_start=invoice_date.year if invoice_date.month>=4 else invoice_date.year-1
    invoice_number=f'{bid:04d}/{str(fy_start)[-2:]}-{str(fy_start+1)[-2:]}'
    currency=settings.get('currency',chr(8377)) or 'INR'
    if currency==chr(8377): currency='Rs.'

    def under_thousand(number):
        ones=['Zero','One','Two','Three','Four','Five','Six','Seven','Eight','Nine','Ten','Eleven','Twelve','Thirteen','Fourteen','Fifteen','Sixteen','Seventeen','Eighteen','Nineteen']
        tens=['','','Twenty','Thirty','Forty','Fifty','Sixty','Seventy','Eighty','Ninety']
        parts=[]
        if number>=100:
            parts.extend([ones[number//100],'Hundred']); number%=100
        if number>=20:
            parts.append(tens[number//10]); number%=10
        if number:
            parts.append(ones[number])
        return ' '.join(parts)

    def amount_in_words(amount):
        rupees=int(amount)
        paise=int(round((amount-rupees)*100))
        if paise==100: rupees+=1; paise=0
        groups=[]
        for divisor,label in ((10000000,'Crore'),(100000,'Lakh'),(1000,'Thousand')):
            count,rupees=divmod(rupees,divisor)
            if count: groups.extend([under_thousand(count),label])
        if rupees or not groups: groups.append(under_thousand(rupees))
        result='Rupees '+' '.join(groups)
        if paise: result+=' and '+under_thousand(paise)+' Paise'
        return result+' Only'

    width,height=A4
    left,right=34,width-34
    table_width=right-left
    col_widths=[44,90,44,55,55,42,58,44,95]
    x_positions=[left]
    for col_width in col_widths: x_positions.append(x_positions[-1]+col_width)
    line_color=HexColor('#777777')
    peach=HexColor('#F4D3B4')
    blue=HexColor('#B8DFEA')
    green=HexColor('#E8F0D9')
    total_blue=HexColor('#9ED5E0')
    lavender=HexColor('#E4DCEF')
    pale=HexColor('#F5F7F8')
    ink=HexColor('#20252A')
    muted=HexColor('#59636D')
    currency_text=lambda value:f'{currency} {value:,.2f}'
    buf=io.BytesIO()
    c=canvas.Canvas(buf,pagesize=A4)

    def draw_company_header(page_number):
        top=height-30
        c.setFillColor(peach); c.setStrokeColor(line_color)
        c.rect(left,top-24,table_width,24,fill=1,stroke=1)
        c.setFillColor(ink); c.setFont('Helvetica',9)
        c.drawString(left+7,top-16,f'Page No. {page_number}')
        c.setFont('Helvetica-Bold',13); c.drawCentredString(width/2,top-16,'Hotel Bill')
        c.setFont('Helvetica',9); c.drawRightString(right-7,top-16,'Original Copy')

        box_top=top-24; box_bottom=box_top-88; logo_width=92
        c.setFillColor(HexColor('#FFFFFF')); c.rect(left,box_bottom,table_width,88,fill=1,stroke=1)
        c.line(left+logo_width,box_bottom,left+logo_width,box_top)
        c.setFillColor(blue); c.circle(left+logo_width/2,(box_top+box_bottom)/2,20,fill=1,stroke=0)
        initials=''.join(word[0] for word in settings.get('hotel_name','Hotel').split() if word)[:3].upper()
        c.setFillColor(ink); c.setFont('Helvetica-Bold',12); c.drawCentredString(left+logo_width/2,(box_top+box_bottom)/2-4,initials or 'H')
        tx=left+logo_width+9; ty=box_top-15
        c.setFillColor(ink); c.setFont('Helvetica-Bold',10)
        c.drawString(tx,ty,f"Hotel Name - {settings.get('hotel_name','Hotel')[:65]}")
        c.setFont('Helvetica',9); ty-=15
        c.drawString(tx,ty,f"Address - {settings.get('address','')[:75] or '-'}"); ty-=14
        c.drawString(tx,ty,f"Mobile - {settings.get('phone','-') or '-'} | Email - {settings.get('email','-') or '-'}"); ty-=14
        c.drawString(tx,ty,f"GSTIN - {settings.get('gstin','-') or '-'} | PAN - {settings.get('pan','-') or '-'}")
        return box_bottom

    def draw_table_header(top):
        row_height=27; bottom=top-row_height
        c.setFillColor(green); c.setStrokeColor(line_color)
        c.rect(left,bottom,table_width,row_height,fill=1,stroke=1)
        headers=['Room No','Name / Item','HSN/SAC','Check In','Check Out','Days / Qty','Price/Day','Tax %','Amount']
        c.setFillColor(ink); c.setFont('Helvetica-Bold',7.2)
        for index,label in enumerate(headers):
            x1=x_positions[index]; x2=x_positions[index+1]
            if index: c.line(x1,bottom,x1,top)
            c.drawCentredString((x1+x2)/2,bottom+10,label)
        return bottom

    current_top=draw_company_header(1)
    detail_top=current_top
    detail_bottom=detail_top-144
    split=left+table_width*.60
    c.setFillColor(blue); c.setStrokeColor(line_color)
    c.rect(left,detail_bottom,table_width,144,fill=1,stroke=1)
    c.line(split,detail_bottom,split,detail_top)
    c.setFillColor(ink); c.setFont('Helvetica-Bold',10)
    c.drawString(left+8,detail_top-16,'Billing Details')
    c.drawString(split+8,detail_top-16,'Invoice Number -')
    c.setFont('Helvetica-Bold',9)
    c.drawRightString(right-8,detail_top-16,invoice_number)
    guest_lines=[('Name',row['guest']),('Address',row['guest_address'] or '-'),('Phone No',row['mobile'] or '-'),('Email ID',row['email'] or '-'),('ID Proof',row['id_proof'] or '-')]
    y=detail_top-37
    c.setFont('Helvetica',8.5)
    for label,value in guest_lines:
        c.drawString(left+8,y,f'{label} - {str(value)[:56]}'); y-=17
    invoice_meta=[('Invoice Date',invoice_date.strftime('%d-%m-%Y')),('Due date',checkout_date.strftime('%d-%m-%Y')),('Booking Status',row['status'])]
    y=detail_top-43
    for label,value in invoice_meta:
        c.setFont('Helvetica-Bold',8.5); c.drawString(split+8,y,f'{label} -')
        c.setFont('Helvetica',8.5); c.drawString(split+100,y,str(value)[:28]); y-=27

    table_top=detail_bottom
    y=draw_table_header(table_top)-1
    checkin_text=checkin_date.strftime('%d-%m-%Y')
    checkout_text=checkout_date.strftime('%d-%m-%Y')
    bill_lines=[(row['room'],f'Room stay - {row["room_type"]}', '-',checkin_text,checkout_text,f'{nights:g}',currency_text(room_rate),f'{tax_rate:.2f}',currency_text(room_subtotal))]
    bill_lines.extend(('-',charge['item_name'],'-','-','-',f'{charge["quantity"]:g}',currency_text(float(charge['unit_price'])),f'{tax_rate:.2f}',currency_text(float(charge['amount']))) for charge in charges)

    def draw_bill_rows(lines,start_y,page_number):
        y=start_y
        for row_index,line in enumerate(lines):
            row_height=27; bottom=y-row_height
            if bottom<110:
                c.showPage(); page_number+=1; header_bottom=draw_company_header(page_number)
                y=header_bottom-18; y=draw_table_header(y)-1; bottom=y-row_height
            c.setFillColor(HexColor('#FFFFFF') if row_index%2==0 else pale)
            c.setStrokeColor(line_color); c.rect(left,bottom,table_width,row_height,fill=1,stroke=1)
            c.setFillColor(ink); c.setFont('Helvetica',7.2)
            for index,value in enumerate(line):
                x1=x_positions[index]; x2=x_positions[index+1]
                if index: c.line(x1,bottom,x1,bottom+row_height)
                display=str(value)
                max_chars=max(5,int((x2-x1)/4.1))
                if index in (6,8): c.drawRightString(x2-3,bottom+10,display[:max_chars])
                else: c.drawCentredString((x1+x2)/2,bottom+10,display[:max_chars])
            y=bottom
        return y,page_number

    y,page_number=draw_bill_rows(bill_lines,y,1)
    if y<245:
        c.showPage(); page_number+=1; y=draw_company_header(page_number)-18
    summary_top=y
    summary_x=left+table_width*.56
    summary_rows=[('Subtotal',currency_text(subtotal))]
    if discount>0: summary_rows.append(('Discount - Returning Guest (5%)',f'- {currency_text(discount)}'))
    summary_rows.extend([(f'Tax ({tax_rate:g}%)',currency_text(tax)),('Rounded Off (+)',f'+ {currency_text(roundoff)[len(currency)+1:]}'),('Total',currency_text(bill_total))])
    summary_height=22
    for label,value in summary_rows:
        bottom=summary_top-summary_height
        c.setFillColor(total_blue if label=='Total' else HexColor('#FFFFFF'))
        c.setStrokeColor(line_color); c.rect(summary_x,bottom,right-summary_x,summary_height,fill=1,stroke=1)
        c.setFillColor(ink); c.setFont('Helvetica-Bold' if label=='Total' else 'Helvetica',9)
        c.drawString(summary_x+7,bottom+7,label[:34]); c.drawRightString(right-7,bottom+7,value)
        summary_top=bottom
    words_y=summary_top-18
    c.setFillColor(ink); c.setFont('Helvetica-Bold',8.5)
    c.drawString(left+3,words_y,f'In Words - {amount_in_words(bill_total)[:85]}')

    note_top=words_y-15
    if note_top<142:
        c.showPage(); page_number+=1; draw_company_header(page_number); note_top=height-105
    note_bottom=92; signature_split=left+table_width*.49; signature_mid=signature_split+(right-signature_split)/2
    c.setFillColor(HexColor('#FFFFFF')); c.setStrokeColor(line_color)
    c.rect(left,note_bottom,table_width,note_top-note_bottom,fill=1,stroke=1)
    c.line(signature_split,note_bottom,signature_split,note_top)
    c.line(signature_mid,note_bottom,signature_mid,note_top)
    c.setFillColor(ink); c.setFont('Helvetica-Bold',8.5); c.drawString(left+5,note_top-14,'Please Note -')
    c.setFont('Helvetica',8)
    c.drawString(left+8,note_top-32,'1. Check-out is available from 12:00 PM on the scheduled date.')
    c.drawString(left+8,note_top-47,'2. Please verify the listed room and guest item charges.')
    c.setFont('Helvetica',8); c.drawCentredString((signature_split+signature_mid)/2,note_top-15,'Billing Officer Signature')
    c.drawCentredString((signature_mid+right)/2,note_top-15,"Guest's Signature")
    footer_y=43
    c.setFillColor(lavender); c.setStrokeColor(line_color); c.rect(left,footer_y,table_width,24,fill=1,stroke=1)
    c.setFillColor(ink); c.setFont('Helvetica-Bold',8.5)
    c.drawCentredString(width/2,footer_y+8,settings.get('invoice_footer','Thank you for staying with us.')[:110])
    c.save(); buf.seek(0)
    return send_file(buf,mimetype='application/pdf',as_attachment=True,download_name=f'hotel-bill-{bid:07d}.pdf')

@app.errorhandler(413)
def too_large(_): return 'Upload too large.',413

if __name__=='__main__':
    init_db()
    # Automatic startup snapshot (one per calendar day).
    if DB.exists() and not any(BACKUPS.glob(f'auto-{date.today().isoformat()}*.db')):
        with connect() as db:
            dst=sqlite3.connect(BACKUPS/f'auto-{date.today().isoformat()}.db'); db.backup(dst); dst.close()
    app.run(host='127.0.0.1',port=5000,debug=False)
