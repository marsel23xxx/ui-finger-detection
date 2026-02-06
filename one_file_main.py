import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import pandas as pd
from scipy.signal import butter, filtfilt, find_peaks
import numpy as np
import time
import serial
import serial.tools.list_ports
import threading
import psycopg2
from psycopg2 import sql
import json

# Global variables
time_data = []
ppg_data = []  # PhotoPlethysmoGram data (AC signal)
ir_data = []   # Threshold values
beat_markers = []  # Beat detection markers
heart_rate_data = []  # Heart rate values
collecting = False
start_time = None
update_needed = False
selected_subject = ""  # Default subject
patient_button = None

# Serial connection
ser = None
serial_thread = None
serial_running = False

# Serial Configuration - EDIT DI SINI!
DEFAULT_PORT = "COM3"  # ← GANTI SESUAI PORT KAMU
DEFAULT_BAUDRATE = 115200

# PostgreSQL Configuration - EDIT DI SINI!
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "heart_rate_db",
    "user": "postgres",
    "password": "marcellganteng"
}

# Database connection
db_conn = None

# Heart rate detection parameters
min_peak_height = 80  # Minimum peak height for AC signal
min_peak_distance = 15  # Minimum distance between peaks (data points) ~0.4s at 50Hz
max_heart_rate = 200  # Maximum realistic heart rate (BPM)
min_heart_rate = 40   # Minimum realistic heart rate (BPM)

# Settling detection - ESP32 butuh 4 detik settling
SETTLING_DURATION = 4.0  # detik
is_settling = False
settling_start_time = None

# GUI components
root = None
fig = None
ax1 = None  # PPG signal plot
ax2 = None  # Heart rate plot
canvas = None
status_label = None
data_count_label = None
latest_data_label = None
data_tree = None
analysis_text = None
port_label = None
db_status_label = None

# subjects = ["Subjek 1", "Subjek 2", "Subjek 3", "Subjek 4", "Subjek 5"]

# Global variables untuk analisis
latest_analysis_text = ""
latest_analysis_data = {}

# ============= SISTEM BUFFERING & AGREGASI DATA =============
# Konfigurasi buffering
BUFFER_SIZE = 50  # Buffer 50 data points sebelum agregasi (~1 detik @ 50Hz)
DOWNSAMPLE_RATE = 10  # Ambil 1 dari setiap 10 data untuk raw data

# Data buffer untuk agregasi
data_buffer = {
    'time': [],
    'ac': [],
    'threshold': [],
    'beat': []
}

# Data teragregasi (lebih ringkas untuk save)
aggregated_data = []  # Format: [{time_avg, ac_avg, ac_min, ac_max, threshold_avg, beat_count, duration}]
raw_downsampled = []  # Raw data yang di-downsample

def aggregate_buffer():
    """Agregasi data dari buffer menjadi summary per interval"""
    global data_buffer, aggregated_data, raw_downsampled
    
    if len(data_buffer['time']) == 0:
        return
    
    # Hitung statistik agregat
    time_start = data_buffer['time'][0]
    time_end = data_buffer['time'][-1]
    time_avg = np.mean(data_buffer['time'])
    
    ac_avg = np.mean(data_buffer['ac'])
    ac_min = np.min(data_buffer['ac'])
    ac_max = np.max(data_buffer['ac'])
    ac_std = np.std(data_buffer['ac'])
    
    threshold_avg = np.mean(data_buffer['threshold'])
    
    # Hitung jumlah beat dalam interval ini
    beat_count = sum(1 for b in data_buffer['beat'] if b > 0)
    
    duration = time_end - time_start
    
    # Simpan data agregat
    aggregated_data.append({
        'time_start': time_start,
        'time_end': time_end,
        'time_avg': time_avg,
        'duration': duration,
        'ac_avg': ac_avg,
        'ac_min': ac_min,
        'ac_max': ac_max,
        'ac_std': ac_std,
        'threshold_avg': threshold_avg,
        'beat_count': beat_count,
        'sample_count': len(data_buffer['time'])
    })
    
    # Simpan beberapa raw data (downsampled) untuk referensi
    for i in range(0, len(data_buffer['time']), DOWNSAMPLE_RATE):
        if i < len(data_buffer['time']):
            raw_downsampled.append({
                'time': data_buffer['time'][i],
                'ac': data_buffer['ac'][i],
                'threshold': data_buffer['threshold'][i],
                'beat': data_buffer['beat'][i]
            })
    
    # Kosongkan buffer
    data_buffer['time'].clear()
    data_buffer['ac'].clear()
    data_buffer['threshold'].clear()
    data_buffer['beat'].clear()

def add_to_buffer(timestamp, ac, threshold, beat):
    """Tambahkan data ke buffer dan agregasi jika sudah penuh"""
    global data_buffer
    
    data_buffer['time'].append(timestamp)
    data_buffer['ac'].append(ac)
    data_buffer['threshold'].append(threshold)
    data_buffer['beat'].append(beat)
    
    # Jika buffer sudah penuh, lakukan agregasi
    if len(data_buffer['time']) >= BUFFER_SIZE:
        aggregate_buffer()

# ===================== DATABASE FUNCTIONS =====================

def connect_database():
    """Connect to PostgreSQL database"""
    global db_conn, DB_CONFIG
    
    try:
        # Close existing connection
        if db_conn and not db_conn.closed:
            db_conn.close()
        
        # Connect to database
        db_conn = psycopg2.connect(**DB_CONFIG)
        
        # Test connection
        cur = db_conn.cursor()
        cur.execute("SELECT version();")
        version = cur.fetchone()
        cur.close()
        
        db_status_label.config(text=f"Database: Terhubung ({DB_CONFIG['database']})", fg="green")
        
        # Create tables if not exist
        create_tables()
        
        messagebox.showinfo("Berhasil", 
                          f"Terhubung ke database PostgreSQL\n\n"
                          f"Host: {DB_CONFIG['host']}\n"
                          f"Database: {DB_CONFIG['database']}\n"
                          f"User: {DB_CONFIG['user']}\n\n"
                          f"Versi: {version[0][:50]}...")
        
        print(f"✅ Connected to PostgreSQL: {DB_CONFIG['database']}")
        return True
        
    except psycopg2.Error as e:
        db_status_label.config(text="Database: Gagal Terhubung", fg="red")
        messagebox.showerror("Error Database", 
                           f"Gagal terhubung ke PostgreSQL:\n\n{str(e)}\n\n"
                           f"Pastikan:\n"
                           f"1. PostgreSQL sudah berjalan\n"
                           f"2. Database '{DB_CONFIG['database']}' sudah dibuat\n"
                           f"3. Username dan password benar\n"
                           f"4. Host dan port sesuai")
        print(f"❌ Database connection failed: {e}")
        return False

def disconnect_database():
    """Disconnect from PostgreSQL database"""
    global db_conn
    
    try:
        if db_conn and not db_conn.closed:
            db_conn.close()
            db_status_label.config(text="Database: Terputus", fg="orange")
            messagebox.showinfo("Info", "Koneksi database terputus")
            print("🔌 Database disconnected")
    except Exception as e:
        messagebox.showerror("Error", f"Gagal memutus koneksi database:\n{str(e)}")

def configure_database():
    """Configure database connection settings"""
    global DB_CONFIG
    
    dialog = tk.Toplevel(root)
    dialog.title("Konfigurasi Database PostgreSQL")
    dialog.geometry("450x400")
    dialog.resizable(False, False)
    dialog.transient(root)
    dialog.grab_set()
    
    frame = tk.Frame(dialog)
    frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
    
    tk.Label(frame, text="Konfigurasi Koneksi PostgreSQL", 
             font=("Arial", 12, "bold")).pack(pady=(0, 15))
    
    # Host
    tk.Label(frame, text="Host:", font=("Arial", 10)).pack(anchor='w')
    host_var = tk.StringVar(value=DB_CONFIG['host'])
    host_entry = tk.Entry(frame, textvariable=host_var, font=("Arial", 10), width=40)
    host_entry.pack(pady=(0, 10))
    
    # Port
    tk.Label(frame, text="Port:", font=("Arial", 10)).pack(anchor='w')
    port_var = tk.StringVar(value=str(DB_CONFIG['port']))
    port_entry = tk.Entry(frame, textvariable=port_var, font=("Arial", 10), width=40)
    port_entry.pack(pady=(0, 10))
    
    # Database
    tk.Label(frame, text="Database:", font=("Arial", 10)).pack(anchor='w')
    database_var = tk.StringVar(value=DB_CONFIG['database'])
    database_entry = tk.Entry(frame, textvariable=database_var, font=("Arial", 10), width=40)
    database_entry.pack(pady=(0, 10))
    
    # User
    tk.Label(frame, text="User:", font=("Arial", 10)).pack(anchor='w')
    user_var = tk.StringVar(value=DB_CONFIG['user'])
    user_entry = tk.Entry(frame, textvariable=user_var, font=("Arial", 10), width=40)
    user_entry.pack(pady=(0, 10))
    
    # Password
    tk.Label(frame, text="Password:", font=("Arial", 10)).pack(anchor='w')
    password_var = tk.StringVar(value=DB_CONFIG['password'])
    password_entry = tk.Entry(frame, textvariable=password_var, font=("Arial", 10), 
                             width=40, show="*")
    password_entry.pack(pady=(0, 20))
    
    def apply_config():
        global DB_CONFIG
        try:
            DB_CONFIG['host'] = host_var.get()
            DB_CONFIG['port'] = int(port_var.get())
            DB_CONFIG['database'] = database_var.get()
            DB_CONFIG['user'] = user_var.get()
            DB_CONFIG['password'] = password_var.get()
            
            messagebox.showinfo("Berhasil", "Konfigurasi database disimpan!\n\nKlik 'Hubungkan Database' untuk menerapkan.")
            dialog.destroy()
        except ValueError:
            messagebox.showerror("Error", "Port harus berupa angka!")
    
    def test_connection():
        try:
            test_config = {
                'host': host_var.get(),
                'port': int(port_var.get()),
                'database': database_var.get(),
                'user': user_var.get(),
                'password': password_var.get()
            }
            
            test_conn = psycopg2.connect(**test_config)
            cur = test_conn.cursor()
            cur.execute("SELECT version();")
            version = cur.fetchone()
            cur.close()
            test_conn.close()
            
            messagebox.showinfo("Berhasil", f"Koneksi berhasil!\n\n{version[0][:100]}")
            
        except Exception as e:
            messagebox.showerror("Gagal", f"Koneksi gagal:\n\n{str(e)}")
    
    button_frame = tk.Frame(frame)
    button_frame.pack(fill=tk.X)
    
    tk.Button(button_frame, text="Test Koneksi", command=test_connection, 
             bg="lightyellow", width=12, font=("Arial", 10)).pack(side=tk.LEFT, padx=(0, 5))
    tk.Button(button_frame, text="Simpan", command=apply_config, 
             bg="lightgreen", width=12, font=("Arial", 10)).pack(side=tk.LEFT, padx=(0, 5))
    tk.Button(button_frame, text="Batal", command=dialog.destroy, 
             bg="lightcoral", width=12, font=("Arial", 10)).pack(side=tk.LEFT)

def create_tables():
    """Create database tables - SIMPLIFIED (no raw data tables!)"""
    global db_conn
    
    if not db_conn or db_conn.closed:
        print("⚠️ Database not connected")
        return False
    
    try:
        cur = db_conn.cursor()
        
        # Table for measurements (metadata pengukuran)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS measurements (
                id SERIAL PRIMARY KEY,
                subject_name VARCHAR(100) NOT NULL,
                measurement_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                duration_seconds FLOAT,
                total_data_points INTEGER,
                sampling_rate FLOAT,
                notes TEXT
            )
        """)
        
        # Table for analysis results (HANYA HASIL AKHIR!)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS analysis_results (
                id SERIAL PRIMARY KEY,
                measurement_id INTEGER REFERENCES measurements(id) ON DELETE CASCADE,
                subject_name VARCHAR(100) NOT NULL,
                avg_heart_rate FLOAT,
                min_heart_rate FLOAT,
                max_heart_rate FLOAT,
                std_heart_rate FLOAT,
                beats_detected INTEGER,
                valid_beats INTEGER,
                hrv_rmssd FLOAT,
                hrv_sdnn FLOAT,
                avg_rr_interval FLOAT,
                classification VARCHAR(100),
                condition VARCHAR(100),
                analysis_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                full_analysis_text TEXT
            )
        """)
        
        # Create index for better performance
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_analysis_measurement 
            ON analysis_results(measurement_id)
        """)
        
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_measurements_date 
            ON measurements(measurement_date DESC)
        """)
        
        db_conn.commit()
        cur.close()
        
        print("✅ Database tables created/verified (ANALYSIS ONLY - NO RAW DATA)")
        return True
        
    except psycopg2.Error as e:
        db_conn.rollback()
        print(f"❌ Error creating tables: {e}")
        messagebox.showerror("Error", f"Gagal membuat tabel database:\n{str(e)}")
        return False

def save_to_database():
    """Save HANYA HASIL ANALISIS ke database - NO RAW DATA!"""
    global db_conn, latest_analysis_data
    
    if not db_conn or db_conn.closed:
        messagebox.showwarning("Peringatan", 
                             "Tidak terhubung ke database!\n\n"
                             "Klik 'Hubungkan Database' terlebih dahulu.")
        return
    
    if not time_data or not ppg_data:
        messagebox.showwarning("Peringatan", "Tidak ada data untuk disimpan!")
        return
    
    # Agregasi sisa buffer (untuk perhitungan, tapi tidak disimpan)
    if len(data_buffer['time']) > 0:
        aggregate_buffer()
    
    if not latest_analysis_data:
        response = messagebox.askyesno("Konfirmasi", 
                                      "Belum ada hasil analisis.\n\n"
                                      "Apakah Anda ingin melakukan analisis terlebih dahulu?")
        if response:
            calculate_heart_rate_statistics()
            if not latest_analysis_data:
                return
        else:
            return
    
    try:
        cur = db_conn.cursor()
        
        # 1. Insert measurement record (metadata saja)
        cur.execute("""
            INSERT INTO measurements 
            (subject_name, duration_seconds, total_data_points, sampling_rate, notes)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (
            selected_subject,
            max(time_data) if time_data else 0,
            len(ppg_data),
            50.0,
            f"Pengukuran detak jantung - HASIL ANALISIS ONLY (no raw data)"
        ))
        
        measurement_id = cur.fetchone()[0]
        
        # 2. Insert HANYA analysis results (NO RAW DATA!)
        cur.execute("""
            INSERT INTO analysis_results 
            (measurement_id, subject_name, avg_heart_rate, min_heart_rate, 
             max_heart_rate, std_heart_rate, beats_detected, valid_beats,
             hrv_rmssd, hrv_sdnn, avg_rr_interval, classification, 
             condition, full_analysis_text)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            measurement_id,
            selected_subject,
            latest_analysis_data.get('avg_hr', 0),
            latest_analysis_data.get('min_hr', 0),
            latest_analysis_data.get('max_hr', 0),
            latest_analysis_data.get('std_hr', 0),
            latest_analysis_data.get('beats_detected', 0),
            latest_analysis_data.get('valid_beats', 0),
            latest_analysis_data.get('rmssd', 0),
            latest_analysis_data.get('sdnn', 0),
            latest_analysis_data.get('avg_rr', 0),
            latest_analysis_data.get('classification', ''),
            latest_analysis_data.get('condition', ''),
            latest_analysis_text
        ))
        
        db_conn.commit()
        cur.close()
        
        messagebox.showinfo("Berhasil", 
                          f"✅ Hasil analisis berhasil disimpan ke database!\n\n"
                          f"Measurement ID: {measurement_id}\n"
                          f"Subjek: {selected_subject}\n"
                          f"─────────────────────────\n"
                          f"Durasi: {max(time_data):.2f} detik\n"
                          f"Detak terdeteksi: {latest_analysis_data.get('beats_detected', 0)}\n"
                          f"HR rata-rata: {latest_analysis_data.get('avg_hr', 0):.1f} BPM\n"
                          f"Klasifikasi: {latest_analysis_data.get('classification', '')}\n"
                          f"HRV (RMSSD): {latest_analysis_data.get('rmssd', 0):.1f} ms\n\n"
                          f"📌 CATATAN:\n"
                          f"Hanya hasil analisis yang disimpan.\n"
                          f"Raw data TIDAK disimpan ke database.")
        
        print(f"✅ Analysis saved to database (ID: {measurement_id})")
        print(f"   📊 HASIL ANALISIS ONLY - No raw data spam!")
        
    except psycopg2.Error as e:
        db_conn.rollback()
        messagebox.showerror("Error", f"Gagal menyimpan ke database:\n\n{str(e)}")
        print(f"❌ Database save error: {e}")

def view_database_records():
    """View saved records from database"""
    global db_conn
    
    if not db_conn or db_conn.closed:
        messagebox.showwarning("Peringatan", "Tidak terhubung ke database!")
        return
    
    try:
        cur = db_conn.cursor()
        
        # Get recent measurements with analysis
        cur.execute("""
            SELECT 
                m.id,
                m.subject_name,
                m.measurement_date,
                m.duration_seconds,
                m.total_data_points,
                a.avg_heart_rate,
                a.beats_detected,
                a.classification,
                a.condition
            FROM measurements m
            LEFT JOIN analysis_results a ON m.id = a.measurement_id
            ORDER BY m.measurement_date DESC
            LIMIT 50
        """)
        
        records = cur.fetchall()
        cur.close()
        
        if not records:
            messagebox.showinfo("Info", "Belum ada data tersimpan di database")
            return
        
        # Create dialog to show records
        dialog = tk.Toplevel(root)
        dialog.title("Hasil Analisis Tersimpan di Database")
        dialog.geometry("1300x600")
        dialog.transient(root)
        
        frame = tk.Frame(dialog)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        tk.Label(frame, text="50 Hasil Analisis Terakhir (HASIL AKHIR ONLY - No Raw Data)", 
                font=("Arial", 12, "bold")).pack(pady=(0, 10))
        
        # Create treeview
        columns = ('ID', 'Subjek', 'Tanggal', 'Durasi', 'Data Points', 'HR Avg', 'Beats', 'Klasifikasi', 'Kondisi')
        tree = ttk.Treeview(frame, columns=columns, show='headings', height=20)
        
        tree.heading('ID', text='ID')
        tree.heading('Subjek', text='Subjek')
        tree.heading('Tanggal', text='Tanggal & Waktu')
        tree.heading('Durasi', text='Durasi (s)')
        tree.heading('Data Points', text='Data Points')
        tree.heading('HR Avg', text='HR Avg (BPM)')
        tree.heading('Beats', text='Beats')
        tree.heading('Klasifikasi', text='Klasifikasi')
        tree.heading('Kondisi', text='Kondisi')
        
        tree.column('ID', width=50)
        tree.column('Subjek', width=100)
        tree.column('Tanggal', width=180)
        tree.column('Durasi', width=80)
        tree.column('Data Points', width=100)
        tree.column('HR Avg', width=100)
        tree.column('Beats', width=80)
        tree.column('Klasifikasi', width=150)
        tree.column('Kondisi', width=120)
        
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        
        scrollbar.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        
        # Insert data
        for record in records:
            tree.insert('', 'end', values=(
                record[0],  # ID
                record[1],  # Subject
                record[2].strftime('%Y-%m-%d %H:%M:%S') if record[2] else '-',
                f"{record[3]:.2f}" if record[3] else '-',
                f"{record[4]}" if record[4] else '-',
                f"{record[5]:.1f}" if record[5] else '-',
                record[6] if record[6] else '-',
                record[7] if record[7] else '-',
                record[8] if record[8] else '-'
            ))
        
        button_frame = tk.Frame(dialog)
        button_frame.pack(fill=tk.X, padx=10, pady=10)
        
        tk.Label(button_frame, text=f"Total: {len(records)} hasil analisis | HASIL AKHIR ONLY (no raw data)", 
                font=("Arial", 10)).pack(side=tk.LEFT)
        
        tk.Button(button_frame, text="Tutup", command=dialog.destroy, 
                 bg="lightgray", width=10).pack(side=tk.RIGHT)
        
    except psycopg2.Error as e:
        messagebox.showerror("Error", f"Gagal membaca database:\n{str(e)}")

# ===================== ORIGINAL FUNCTIONS (dengan modifikasi buffering) =====================

def list_serial_ports():
    """List all available serial ports"""
    ports = serial.tools.list_ports.comports()
    available_ports = []
    for port in ports:
        available_ports.append(port.device)
    return available_ports

def connect_serial_auto():
    """Auto connect to default port"""
    global ser, serial_running, serial_thread
    
    port = DEFAULT_PORT
    baudrate = DEFAULT_BAUDRATE
    
    try:
        # Close existing connection
        if ser and ser.is_open:
            ser.close()
            time.sleep(0.5)
        
        # Open new connection
        ser = serial.Serial(port, baudrate, timeout=1)
        time.sleep(2)  # Wait for Arduino to reset
        
        # Clear buffer
        ser.reset_input_buffer()
        
        status_label.config(text=f"Status: Terhubung ke {port}", fg="green")
        port_label.config(text=f"Port: {port} @ {baudrate} baud")
        
        # Start serial reading thread
        serial_running = True
        serial_thread = threading.Thread(target=read_serial_data, daemon=True)
        serial_thread.start()
        
        print(f"✅ Auto-connected to {port} @ {baudrate} baud")
        messagebox.showinfo("Berhasil", f"Terhubung otomatis ke {port}\nBaudrate: {baudrate}\n\nSiap untuk pengukuran!")
        
        return True
        
    except serial.SerialException as e:
        print(f"❌ Gagal terhubung ke {port}: {e}")
        status_label.config(text="Status: Koneksi Gagal", fg="red")
        port_label.config(text="Port: Tidak Terhubung")
        messagebox.showerror("Error", f"Gagal terhubung ke {port}:\n{str(e)}\n\nPastikan:\n1. ESP32 terhubung ke {port}\n2. Arduino IDE Serial Monitor sudah ditutup\n3. Port tidak digunakan aplikasi lain")
        return False

def select_serial_port():
    """Select serial port from available ports"""
    ports = list_serial_ports()
    
    if not ports:
        messagebox.showerror("Error", "Tidak ada port serial yang tersedia!\n\nPastikan ESP32 terhubung ke komputer.")
        return None, None
    
    dialog = tk.Toplevel(root)
    dialog.title("Pilih Port Serial")
    dialog.geometry("400x300")
    dialog.resizable(False, False)
    dialog.transient(root)
    dialog.grab_set()
    
    frame = tk.Frame(dialog)
    frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
    
    tk.Label(frame, text="Pilih Port Serial ESP32:", 
             font=("Arial", 11, "bold")).pack(pady=(0, 10))
    
    # Set default to COM3 if available, otherwise first port
    default_port = DEFAULT_PORT if DEFAULT_PORT in ports else ports[0]
    port_var = tk.StringVar(value=default_port)
    
    port_dropdown = ttk.Combobox(frame, textvariable=port_var, 
                                values=ports, state="readonly", 
                                font=("Arial", 10), width=30)
    port_dropdown.pack(pady=(0, 10))
    
    # Show available ports
    tk.Label(frame, text="Port yang tersedia:", font=("Arial", 9)).pack(pady=(10, 5))
    ports_text = tk.Text(frame, height=4, width=40, font=("Courier", 8))
    ports_text.pack(pady=(0, 10))
    for p in ports:
        ports_text.insert(tk.END, f"• {p}\n")
    ports_text.config(state=tk.DISABLED)
    
    tk.Label(frame, text="Baudrate:", font=("Arial", 10)).pack(pady=(10, 5))
    baudrate_var = tk.StringVar(value=str(DEFAULT_BAUDRATE))
    baudrate_entry = tk.Entry(frame, textvariable=baudrate_var, 
                             font=("Arial", 10), width=15)
    baudrate_entry.pack(pady=(0, 20))
    
    selected_port = [None]
    selected_baudrate = [None]
    
    def apply_selection():
        selected_port[0] = port_var.get()
        try:
            selected_baudrate[0] = int(baudrate_var.get())
        except ValueError:
            messagebox.showerror("Error", "Baudrate harus berupa angka!")
            return
        dialog.destroy()
    
    def cancel_selection():
        dialog.destroy()
    
    button_frame = tk.Frame(frame)
    button_frame.pack(fill=tk.X)
    
    tk.Button(button_frame, text="Terapkan", command=apply_selection, 
             bg="lightgreen", width=10, font=("Arial", 10)).pack(side=tk.LEFT, padx=(0, 10))
    tk.Button(button_frame, text="Batal", command=cancel_selection, 
             bg="lightcoral", width=10, font=("Arial", 10)).pack(side=tk.LEFT)
    
    dialog.wait_window()
    
    if selected_port[0] and selected_baudrate[0]:
        return selected_port[0], selected_baudrate[0]
    return None, None

def connect_serial():
    """Connect to serial port (manual selection)"""
    global ser, serial_running, serial_thread
    
    port, baudrate = select_serial_port()
    
    if not port:
        return
    
    try:
        # Close existing connection
        if ser and ser.is_open:
            ser.close()
            time.sleep(0.5)
        
        # Open new connection
        ser = serial.Serial(port, baudrate, timeout=1)
        time.sleep(2)  # Wait for Arduino to reset
        
        # Clear buffer
        ser.reset_input_buffer()
        
        status_label.config(text=f"Status: Terhubung ke {port}", fg="green")
        port_label.config(text=f"Port: {port} @ {baudrate} baud")
        
        # Start serial reading thread
        serial_running = True
        serial_thread = threading.Thread(target=read_serial_data, daemon=True)
        serial_thread.start()
        
        messagebox.showinfo("Berhasil", f"Terhubung ke {port}\nBaudrate: {baudrate}")
        
    except serial.SerialException as e:
        messagebox.showerror("Error", f"Gagal terhubung ke {port}:\n{str(e)}")
        status_label.config(text="Status: Koneksi Gagal", fg="red")
        port_label.config(text="Port: Tidak Terhubung")

def disconnect_serial():
    """Disconnect from serial port"""
    global ser, serial_running
    
    serial_running = False
    
    if ser and ser.is_open:
        try:
            ser.close()
            status_label.config(text="Status: Terputus", fg="orange")
            port_label.config(text="Port: Tidak Terhubung")
            messagebox.showinfo("Info", "Koneksi serial terputus")
        except Exception as e:
            messagebox.showerror("Error", f"Gagal memutus koneksi:\n{str(e)}")

def read_serial_data():
    """Read data from serial port - OPTIMIZED UNTUK ESP32 BARU!"""
    global time_data, ppg_data, ir_data, beat_markers, heart_rate_data
    global collecting, start_time, update_needed, ser
    global is_settling, settling_start_time
    
    print("🔄 Serial reading thread started (ESP32 OPTIMIZED)")
    
    while serial_running:
        if not ser or not ser.is_open:
            break
            
        try:
            if ser.in_waiting > 0:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                
                if not line or not collecting:
                    continue
                
                # Parse data: "AC THRESHOLD BEAT_MARKER"
                parts = line.split()
                
                if len(parts) >= 3:
                    try:
                        ac = int(parts[0])
                        threshold = int(parts[1])
                        beat_marker = int(parts[2])
                        
                        # Initialize start time
                        if start_time is None:
                            start_time = time.time()
                            settling_start_time = start_time
                            is_settling = True
                            print("⏳ Settling period started (4 detik)...")
                        
                        current_time = time.time() - start_time
                        
                        # ===== CEK SETTLING PERIOD =====
                        if is_settling and (current_time >= SETTLING_DURATION):
                            is_settling = False
                            print("✅ Settling done! Beat detection active.")
                            root.after(0, lambda: status_label.config(
                                text=f"Status: Ready - {selected_subject}", 
                                fg="green"
                            ))
                        
                        # Store data ke array asli (untuk plotting real-time)
                        time_data.append(float(current_time))
                        ppg_data.append(float(ac))
                        ir_data.append(float(threshold))
                        beat_markers.append(float(beat_marker))
                        
                        # ===== BUFFER HANYA DATA READY (setelah settling) =====
                        if not is_settling:  # ← FILTER settling data!
                            add_to_buffer(current_time, ac, threshold, beat_marker)
                        
                        # Calculate heart rate from ESP32 beat markers
                        # HANYA PROSES BEAT SETELAH SETTLING!
                        if beat_marker > 0 and not is_settling:  # ← TAMBAH check settling!
                            # Find recent beats
                            recent_beats = []
                            for i in range(len(beat_markers) - 1, max(0, len(beat_markers) - 10), -1):
                                if beat_markers[i] > 0:
                                    recent_beats.append(time_data[i])
                            
                            # Calculate BPM from last 2 beats
                            if len(recent_beats) >= 2:
                                interval = recent_beats[0] - recent_beats[1]
                                if interval > 0:
                                    bpm = 60.0 / interval
                                    if min_heart_rate <= bpm <= max_heart_rate:
                                        heart_rate_data.append(bpm)
                                        print(f"💓 Beat! BPM: {bpm:.1f} (ESP32)")
                        
                        update_needed = True
                        
                        # Update GUI labels
                        if len(ppg_data) % 10 == 0:
                            settling_status = "SETTLING..." if is_settling else "READY"
                            root.after(0, lambda: data_count_label.config(
                                text=f"Data: {len(ppg_data)} | Buffer: {len(data_buffer['time'])} | Agregat: {len(aggregated_data)} | {settling_status}"
                            ))
                            root.after(0, lambda: latest_data_label.config(
                                text=f"AC: {ac}, Beat: {'YA' if beat_marker > 0 else 'TIDAK'}, Time: {current_time:.2f}s"
                            ))
                        
                    except ValueError as e:
                        print(f"⚠️ Parse error: {line} -> {e}")
                        continue
                
        except Exception as e:
            print(f"❌ Serial read error: {e}")
            if not serial_running:
                break
            time.sleep(0.01)
    
    print("🛑 Serial reading thread stopped")

def bandpass_filter(data, lowcut=0.5, highcut=5.0, fs=50, order=4):
    """Apply bandpass filter to PPG signal (0.5-5 Hz for heart rate)"""
    min_length = max(order * 6, 20)
    if len(data) < min_length:
        return data
    
    try:
        nyq = 0.5 * fs
        low = lowcut / nyq
        high = highcut / nyq
        b, a = butter(order, [low, high], btype='band', analog=False)
        return filtfilt(b, a, data)
    except ValueError as e:
        print(f"Filter error: {e}")
        return data

def detect_heartbeats(ppg_data, time_data, min_height=None, min_distance=None):
    """Detect heartbeat peaks from PPG signal"""
    if len(ppg_data) < 50:
        return [], [], []
    
    if min_height is None:
        min_height = min_peak_height
    if min_distance is None:
        min_distance = min_peak_distance
    
    try:
        # Apply bandpass filter
        filtered_ppg = bandpass_filter(ppg_data)
        
        # Find peaks in PPG signal
        peaks, properties = find_peaks(filtered_ppg, 
                                     height=min_height,
                                     distance=min_distance,
                                     prominence=10)
        
        peak_times = [time_data[p] for p in peaks if p < len(time_data)]
        peak_values = [filtered_ppg[p] for p in peaks if p < len(filtered_ppg)]
        
        # Calculate heart rate from peak intervals
        heart_rates = []
        if len(peak_times) >= 2:
            for i in range(1, len(peak_times)):
                interval = peak_times[i] - peak_times[i-1]  # seconds
                if interval > 0:
                    bpm = 60.0 / interval
                    # Validate heart rate
                    if min_heart_rate <= bpm <= max_heart_rate:
                        heart_rates.append(bpm)
                    else:
                        heart_rates.append(None)
        
        print(f"Detected {len(peaks)} heartbeats, valid HR: {len([h for h in heart_rates if h is not None])}")
        
        return peak_times, peak_values, heart_rates
    except Exception as e:
        print(f"Heartbeat detection error: {e}")
        return [], [], []

def set_subject():
    """Set subject dengan input manual (bukan dropdown)"""
    global selected_subject, update_needed
    
    dialog = tk.Toplevel(root)
    dialog.title("Atur Nama Pasien")
    dialog.geometry("400x200")
    dialog.resizable(False, False)
    
    dialog.transient(root)
    dialog.grab_set()
    
    frame = tk.Frame(dialog)
    frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
    
    tk.Label(frame, text="Masukkan nama pasien yang akan diukur:", 
             font=("Arial", 11, "bold")).pack(pady=(0, 10))
    
    # Input text biasa (bukan dropdown!)
    subject_var = tk.StringVar(value=selected_subject)
    
    subject_entry = tk.Entry(frame, textvariable=subject_var, 
                            font=("Arial", 12), width=30)
    subject_entry.pack(pady=(0, 10))
    subject_entry.focus()  # Auto focus ke input
    
    # Hint text
    tk.Label(frame, text="Contoh: Budi Santoso, Pasien 001, dll.", 
            font=("Arial", 9, "italic"), fg="gray").pack(pady=(0, 20))
    
    button_frame = tk.Frame(frame)
    button_frame.pack(fill=tk.X)
    
    def apply_selection():
        global selected_subject, update_needed, patient_button  # ← TAMBAH patient_button
        nama = subject_var.get().strip()
        
        if not nama:
            messagebox.showwarning("Peringatan", "Nama pasien tidak boleh kosong!")
            return
        
        selected_subject = nama
        update_needed = True
        status_label.config(text=f"Status: Pasien diatur ke {selected_subject}", fg="blue")
        
        # ✨ UPDATE BUTTON TEXT!
        if patient_button:
            patient_button.config(text=f"Pasien: {selected_subject}")
        
        print(f"Pasien diubah ke: {selected_subject}")
        dialog.destroy()
    
    def cancel_selection():
        dialog.destroy()
    
    # Bind Enter key
    subject_entry.bind('<Return>', lambda e: apply_selection())
    
    tk.Button(button_frame, text="Terapkan", command=apply_selection, 
             bg="lightgreen", width=10, font=("Arial", 10)).pack(side=tk.LEFT, padx=(0, 10))
    tk.Button(button_frame, text="Batal", command=cancel_selection, 
             bg="lightcoral", width=10, font=("Arial", 10)).pack(side=tk.LEFT)

def calculate_heart_rate_statistics():
    """Calculate heart rate statistics and variability"""
    global latest_analysis_text, latest_analysis_data
    
    if len(ppg_data) < 100:
        messagebox.showwarning("Peringatan", "Tidak cukup data untuk analisis\nMinimum diperlukan: 100 titik data (2 detik)")
        return
    
    try:
        peak_times, peak_values, heart_rates = detect_heartbeats(ppg_data, time_data)
        
        valid_heart_rates = [hr for hr in heart_rates if hr is not None]
        
        if len(valid_heart_rates) < 2:
            messagebox.showwarning("Peringatan", 
                                 f"Perlu setidaknya 2 detak jantung untuk analisis\n"
                                 f"Saat ini terdeteksi: {len(peak_times)} detak\n"
                                 f"Valid HR: {len(valid_heart_rates)}\n"
                                 f"Coba sesuaikan pengaturan deteksi")
            return
        
        # Calculate statistics
        avg_hr = np.mean(valid_heart_rates)
        std_hr = np.std(valid_heart_rates)
        min_hr = np.min(valid_heart_rates)
        max_hr = np.max(valid_heart_rates)
        
        # Calculate RR intervals (time between heartbeats in ms)
        rr_intervals = []
        if len(peak_times) >= 2:
            for i in range(1, len(peak_times)):
                rr = (peak_times[i] - peak_times[i-1]) * 1000  # convert to ms
                rr_intervals.append(rr)
        
        # Heart Rate Variability (HRV) metrics
        if len(rr_intervals) >= 2:
            rmssd = np.sqrt(np.mean(np.diff(rr_intervals)**2))  # Root Mean Square of Successive Differences
            sdnn = np.std(rr_intervals)  # Standard Deviation of NN intervals
        else:
            rmssd = 0
            sdnn = 0
        
        avg_rr = np.mean(rr_intervals) if rr_intervals else 0
        
        # Classify heart rate
        if avg_hr < 60:
            hr_classification = "Bradikardia (Lambat)"
            condition = "Di bawah normal"
        elif 60 <= avg_hr <= 100:
            hr_classification = "Normal"
            condition = "Sehat"
        else:
            hr_classification = "Takikardia (Cepat)"
            condition = "Di atas normal"
        
        # Store analysis data for database
        latest_analysis_data = {
            'avg_hr': avg_hr,
            'min_hr': min_hr,
            'max_hr': max_hr,
            'std_hr': std_hr,
            'beats_detected': len(peak_times),
            'valid_beats': len(valid_heart_rates),
            'rmssd': rmssd,
            'sdnn': sdnn,
            'avg_rr': avg_rr,
            'classification': hr_classification,
            'condition': condition
        }
        
        # Format comprehensive results
        latest_analysis_text = f"""
{'='*60}
    ANALISIS DETAK JANTUNG - {selected_subject.upper()}
{'='*60}

INFORMASI PENGUKURAN:
  • Subjek                       : {selected_subject}
  • Durasi Pengukuran            : {max(time_data):.2f} detik
  • Total Titik Data             : {len(ppg_data)}
  • Data Agregat Tersimpan       : {len(aggregated_data)} records
  • Sampling Rate                : ~50 Hz
  • Efisiensi Penyimpanan        : {(1 - len(aggregated_data)/max(1, len(ppg_data)))*100:.1f}%

{'─'*60}
HASIL DETEKSI DETAK JANTUNG:
  • Jumlah Detak Terdeteksi      : {len(peak_times)}
  • Detak Valid                  : {len(valid_heart_rates)}
  • Waktu Detak (s)              : {', '.join([f'{t:.2f}' for t in peak_times[:10]])}{'...' if len(peak_times) > 10 else ''}
  • RR Intervals (ms)            : {', '.join([f'{rr:.0f}' for rr in rr_intervals[:10]])}{'...' if len(rr_intervals) > 10 else ''}

{'─'*60}
STATISTIK DETAK JANTUNG:
  • Detak Jantung Rata-rata      : {avg_hr:.1f} BPM
  • Detak Jantung Minimum        : {min_hr:.1f} BPM
  • Detak Jantung Maksimum       : {max_hr:.1f} BPM
  • Standar Deviasi              : {std_hr:.2f} BPM
  • Rentang                      : {max_hr - min_hr:.1f} BPM

{'─'*60}
HEART RATE VARIABILITY (HRV):
  • SDNN (Standar Deviasi RR)    : {sdnn:.2f} ms
  • RMSSD (Root Mean Square)     : {rmssd:.2f} ms
  • Rata-rata RR Interval        : {avg_rr:.1f} ms

INTERPRETASI HRV:
  • RMSSD Tinggi (>50ms)         : Sistem saraf parasimpatik aktif (relaks)
  • RMSSD Rendah (<20ms)         : Stres atau kelelahan
  • Anda                         : {rmssd:.1f} ms ({'Baik' if rmssd > 50 else 'Perhatian' if rmssd > 20 else 'Rendah'})

{'─'*60}
KLASIFIKASI DETAK JANTUNG:
  • Kategori                     : {hr_classification}
  • Kondisi                      : {condition}
  • Status Kesehatan             : {'Normal' if 60 <= avg_hr <= 100 else 'Perlu Perhatian'}

{'─'*60}
DATA STORAGE INFO (BUFFERED):
  • Total Data Asli              : {len(ppg_data)} points
  • Data Agregat                 : {len(aggregated_data)} records
  • Raw Downsampled              : {len(raw_downsampled)} samples
  • Efisiensi                    : {(1 - len(aggregated_data)/max(1, len(ppg_data)))*100:.1f}% pengurangan spam!

{'='*60}
Analisis {selected_subject} selesai pada: {time.strftime('%Y-%m-%d %H:%M:%S')}
Durasi: 0.00 - {max(time_data):.2f} detik
{'='*60}
"""
        
        update_analysis_display()
        
        messagebox.showinfo("Berhasil", 
                          f"Analisis detak jantung {selected_subject} selesai!\n\n"
                          f"Detak terdeteksi: {len(peak_times)}\n"
                          f"Detak jantung rata-rata: {avg_hr:.1f} BPM\n"
                          f"Kategori: {hr_classification}\n"
                          f"HRV (RMSSD): {rmssd:.1f} ms\n"
                          f"Durasi: {max(time_data):.2f} detik\n\n"
                          f"📊 Data Buffering:\n"
                          f"Asli: {len(ppg_data)} → Agregat: {len(aggregated_data)}\n"
                          f"Efisiensi: {(1-len(aggregated_data)/max(1,len(ppg_data)))*100:.1f}% lebih ringkas!\n\n"
                          f"Hasil lengkap di panel Analisis")
        
    except Exception as e:
        messagebox.showerror("Error", f"Error analisis:\n{str(e)}")
        print(f"Calculation error: {e}")

def start_collection():
    """Start data collection"""
    global collecting, start_time, update_needed, is_settling, settling_start_time
    
    if not ser or not ser.is_open:
        messagebox.showwarning("Peringatan", "Tidak terhubung ke port serial!\n\nKlik 'Hubungkan Serial' terlebih dahulu.")
        return
    
    collecting = True
    start_time = time.time()
    settling_start_time = start_time
    is_settling = True  # ← Reset settling flag
    update_needed = True
    
    status_label.config(text="Status: Settling Period (4s) - Tunggu...", fg="orange")
    print("▶️ Pengumpulan data dimulai (SETTLING 4 detik...)")
    print("⏳ Jari harus tetap di sensor selama settling!")

def stop_collection():
    """Stop data collection"""
    global collecting, update_needed
    collecting = False
    update_needed = True
    
    # Agregasi sisa buffer
    if len(data_buffer['time']) > 0:
        aggregate_buffer()
        print(f"✅ Final buffer agregasi: {len(aggregated_data)} total records")
    
    status_label.config(text="Status: Berhenti", fg="red")
    print("⏸️ Pengumpulan data dihentikan")
    
    if len(ppg_data) >= 100:
        root.after(1000, calculate_heart_rate_statistics)

def reset_data():
    """Reset all collected data"""
    global time_data, ppg_data, ir_data, beat_markers, heart_rate_data, start_time, update_needed
    global latest_analysis_text, latest_analysis_data
    global data_buffer, aggregated_data, raw_downsampled
    global is_settling, settling_start_time  # ← TAMBAH ini
    
    time_data.clear()
    ppg_data.clear()
    ir_data.clear()
    beat_markers.clear()
    heart_rate_data.clear()
    start_time = None
    settling_start_time = None  # ← RESET settling
    is_settling = False  # ← RESET settling
    update_needed = True
    latest_analysis_text = ""
    latest_analysis_data = {}
    
    # Reset buffer data
    data_buffer['time'].clear()
    data_buffer['ac'].clear()
    data_buffer['threshold'].clear()
    data_buffer['beat'].clear()
    aggregated_data.clear()
    raw_downsampled.clear()
    
    data_count_label.config(text="Data: 0 | Buffer: 0 | Agregat: 0")
    latest_data_label.config(text="Terbaru: -")
    status_label.config(text="Status: Data Direset", fg="blue")
    update_plot()
    update_analysis_display()
    print("🔄 Data direset (termasuk settling period)")

def update_data_table():
    """Update the data table"""
    global data_tree
    
    try:
        for item in data_tree.get_children():
            data_tree.delete(item)
        
        if not time_data or not ppg_data:
            return
        
        start_idx = max(0, len(time_data) - 50)
        
        for i in range(start_idx, len(time_data)):
            if i < len(time_data) and i < len(ppg_data):
                t = time_data[i]
                ppg = ppg_data[i]
                threshold = ir_data[i] if i < len(ir_data) else 0
                beat = "YA" if (i < len(beat_markers) and beat_markers[i] > 0) else "TIDAK"
                
                data_tree.insert('', 'end', values=(
                    i + 1,
                    f"{float(t):.2f}",
                    f"{float(ppg):.0f}",
                    f"{float(threshold):.0f}",
                    beat
                ))
        
        if data_tree.get_children():
            data_tree.see(data_tree.get_children()[-1])
            
    except Exception as e:
        print(f"Error updating table: {e}")

def save_analysis_to_file():
    """Save analysis results"""
    global latest_analysis_text
    
    if not latest_analysis_text:
        messagebox.showwarning("Peringatan", "Tidak ada hasil analisis untuk disimpan")
        return
    
    try:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        subject_name = selected_subject.replace(" ", "_")
        default_filename = f"Analisis_HR_{subject_name}_{timestamp}.txt"
        
        file_path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("File Teks", "*.txt"), ("Semua file", "*.*")],
            title="Simpan Hasil Analisis",
            initialfile=default_filename
        )
        
        if file_path:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(latest_analysis_text)
            
            messagebox.showinfo("Berhasil", f"Analisis disimpan ke {file_path}")
            
    except Exception as e:
        messagebox.showerror("Error", f"Gagal menyimpan:\n{str(e)}")

def update_analysis_display():
    """Update analysis display"""
    global analysis_text, latest_analysis_text, is_settling
    
    if len(ppg_data) < 2:
        analysis_info = "Tidak ada data untuk analisis"
    else:
        if latest_analysis_text:
            analysis_info = latest_analysis_text
        else:
            peak_times, peak_values, heart_rates = detect_heartbeats(ppg_data, time_data)
            valid_hrs = [hr for hr in heart_rates if hr is not None]
            
            # ← TAMBAH status settling
            settling_status = "⏳ SETTLING (tunggu 4s)" if is_settling else "✅ READY"
            
            analysis_info = f"""ANALISIS REAL-TIME - {selected_subject.upper()}
{"="*30}

STATUS: {settling_status}  # ← TAMPILKAN SETTLING!
SUBJEK: {selected_subject}

STATISTIK DATA:
- Jumlah Data Total: {len(ppg_data)}
- Data Ready (post-settling): {len(aggregated_data) * BUFFER_SIZE}
- Data Agregat: {len(aggregated_data)}
- Buffer Aktif: {len(data_buffer['time'])}
- Durasi: {max(time_data) if time_data else 0:.1f}s
- AC Min: {min(ppg_data) if ppg_data else 0:.0f}
- AC Max: {max(ppg_data) if ppg_data else 0:.0f}
- AC Rata-rata: {np.mean(ppg_data) if ppg_data else 0:.0f}

EFISIENSI BUFFERING:
- Pengurangan: {(1 - len(aggregated_data)/max(1, len(ppg_data)))*100:.1f}%
- Raw Downsampled: {len(raw_downsampled)}

DETEKSI DETAK (ESP32):
- Detak Terdeteksi: {len(peak_times)}
- HR Valid: {len(valid_hrs)}
- HR Rata-rata: {np.mean(valid_hrs) if valid_hrs else 0:.1f} BPM
- Threshold ESP32: 80

PENGATURAN:
- Buffer Size: {BUFFER_SIZE}
- Downsample Rate: 1/{DOWNSAMPLE_RATE}
- Settling: {'AKTIF' if is_settling else 'SELESAI'}
- Status: {'Collecting' if collecting else 'Stopped'}

CATATAN:
{'⚠️ Data settling (4s pertama) tidak disimpan ke buffer!' if is_settling else '✅ Data ready untuk analisis!'}

Klik "Analisis Detak Jantung" 
untuk hasil lengkap
"""
    
    analysis_text.config(state=tk.NORMAL)
    analysis_text.delete(1.0, tk.END)
    analysis_text.insert(1.0, analysis_info)
    analysis_text.config(state=tk.DISABLED)

def update_plot():
    """Update matplotlib plots - FULL VERSION with SETTLING indicator"""
    global is_settling
    
    try:
        if len(time_data) == 0:
            ax1.clear()
            ax2.clear()
            ax1.set_xlabel("Waktu (detik)")
            ax1.set_ylabel("Amplitudo AC")
            ax1.set_title(f"Sinyal AC (Filtered) - {selected_subject}")
            ax1.grid(True, alpha=0.3)
            
            ax2.set_xlabel("Waktu (detik)")
            ax2.set_ylabel("Detak Jantung (BPM)")
            ax2.set_title("Detak Jantung Real-time")
            ax2.grid(True, alpha=0.3)
            canvas.draw_idle()
            return
        
        # ============= PLOT 1: AC SIGNAL =============
        ax1.clear()
        
        # Apply filter if enough data
        if len(ppg_data) > 50:
            filtered_ppg = bandpass_filter(ppg_data)
        else:
            filtered_ppg = ppg_data
        
        # Plot AC signal
        ax1.plot(time_data, filtered_ppg, 'r-', label="Sinyal AC (Filtered)", linewidth=1.5)
        
        # Plot threshold line
        if ir_data:
            ax1.plot(time_data, ir_data[:len(time_data)], 'b--', label="Threshold (80)", linewidth=1, alpha=0.7)
        
        # ===== VISUAL SETTLING ZONE =====
        if is_settling or (len(time_data) > 0 and max(time_data) < SETTLING_DURATION):
            settling_end = min(SETTLING_DURATION, max(time_data) if time_data else 0)
            ax1.axvspan(0, settling_end, alpha=0.2, color='yellow', label='Settling Zone')
            if max(time_data) >= SETTLING_DURATION:
                ax1.axvline(x=SETTLING_DURATION, color='green', linestyle=':', linewidth=2, label='Ready!')
        
        # Mark beats from ESP32
        beat_times = [time_data[i] for i in range(len(beat_markers)) 
                     if i < len(time_data) and beat_markers[i] > 0]
        beat_values = [filtered_ppg[i] for i in range(len(beat_markers)) 
                      if i < len(filtered_ppg) and beat_markers[i] > 0]
        
        if beat_times and beat_values:
            ax1.plot(beat_times, beat_values, "go", label="Beat ESP32", markersize=8, 
                    markeredgecolor='black', markeredgewidth=1)
        
        # Detect and mark peaks using Python (optional, for comparison)
        if len(filtered_ppg) > 50:
            peak_times, peak_values, heart_rates = detect_heartbeats(filtered_ppg, time_data)
            if peak_times and peak_values:
                ax1.plot(peak_times, peak_values, "m^", label="Detak Python", markersize=6, alpha=0.7)
        
        # Title dengan status settling
        title_suffix = " [⏳ SETTLING...]" if is_settling else " [✅ READY]"
        ax1.set_xlabel("Waktu (detik)", fontsize=10)
        ax1.set_ylabel("Amplitudo AC", fontsize=10)
        ax1.set_title(f"Sinyal AC - {selected_subject}{title_suffix}", fontsize=11, fontweight='bold')
        ax1.legend(loc='upper right', fontsize=8)
        ax1.grid(True, alpha=0.3)
        
        # ============= PLOT 2: HEART RATE =============
        ax2.clear()
        
        if len(filtered_ppg) > 50:
            peak_times, peak_values, heart_rates = detect_heartbeats(filtered_ppg, time_data)
            valid_hrs = [(peak_times[i+1], hr) for i, hr in enumerate(heart_rates) if hr is not None]
            
            if valid_hrs:
                hr_times = [t for t, hr in valid_hrs]
                hr_values = [hr for t, hr in valid_hrs]
                ax2.plot(hr_times, hr_values, 'b-o', label="Detak Jantung", linewidth=2, markersize=4)
                
                # Add reference lines
                ax2.axhline(y=60, color='g', linestyle='--', alpha=0.5, label='Normal Min (60)')
                ax2.axhline(y=100, color='r', linestyle='--', alpha=0.5, label='Normal Max (100)')
                
                # Settling zone di plot HR juga
                if is_settling or (len(time_data) > 0 and max(time_data) < SETTLING_DURATION):
                    settling_end = min(SETTLING_DURATION, max(time_data) if time_data else 0)
                    ax2.axvspan(0, settling_end, alpha=0.2, color='yellow')
                    if max(time_data) >= SETTLING_DURATION:
                        ax2.axvline(x=SETTLING_DURATION, color='green', linestyle=':', linewidth=2)
        
        ax2.set_xlabel("Waktu (detik)", fontsize=10)
        ax2.set_ylabel("Detak Jantung (BPM)", fontsize=10)
        
        # Title dengan info agregat
        hr_title = f"Detak Jantung Real-time [{len(aggregated_data)} agregat]"
        if is_settling:
            hr_title += " [⏳ WAITING...]"
        ax2.set_title(hr_title, fontsize=11, fontweight='bold')
        
        ax2.legend(loc='upper right', fontsize=8)
        ax2.grid(True, alpha=0.3)
        ax2.set_ylim(30, 120)
        
        # Draw canvas
        canvas.draw_idle()
        
        # Update data table dan analysis
        update_data_table()
        update_analysis_display()
        
    except Exception as e:
        print(f"❌ Plot error: {e}")
        import traceback
        traceback.print_exc()

def periodic_update():
    """Periodic update for real-time display"""
    global update_needed
    
    try:
        if update_needed and len(time_data) >= 0:
            update_plot()
            update_needed = False
        
        root.update_idletasks()
        
    except Exception as e:
        print(f"Update error: {e}")
    
    interval = 50 if collecting or len(time_data) > 0 else 200
    root.after(interval, periodic_update)

def save_excel():
    """Save data to Excel - VERSI BUFFERED (DATA AGREGAT)"""
    if not time_data or not ppg_data:
        messagebox.showwarning("Peringatan", "Tidak ada data")
        return
    
    # Agregasi sisa buffer jika ada
    if len(data_buffer['time']) > 0:
        aggregate_buffer()
    
    try:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        subject_name = selected_subject.replace(" ", "_")
        default_filename = f"Data_HR_{subject_name}_{timestamp}_BUFFERED.xlsx"
        
        file_path = filedialog.asksaveasfilename(
            defaultextension=".xlsx", 
            filetypes=[("File Excel", "*.xlsx")],
            title="Simpan Data (Buffered)",
            initialfile=default_filename
        )
        
        if file_path:
            # Sheet 1: Data Agregat (lebih ringkas, tidak spam!)
            df_aggregated = pd.DataFrame(aggregated_data)
            df_aggregated['subjek'] = selected_subject
            
            # Sheet 2: Raw Data Downsampled (untuk referensi)
            df_raw = pd.DataFrame(raw_downsampled)
            df_raw['subjek'] = selected_subject
            
            # Sheet 3: Summary
            summary_data = {
                'Parameter': [
                    'Subjek',
                    'Total Data Points Asli',
                    'Total Agregat Records',
                    'Total Raw Downsampled',
                    'Efisiensi Penyimpanan (%)',
                    'Durasi (detik)',
                    'Sampling Rate (Hz)',
                    'Timestamp'
                ],
                'Nilai': [
                    selected_subject,
                    len(ppg_data),
                    len(aggregated_data),
                    len(raw_downsampled),
                    f"{(1 - len(aggregated_data)/max(1, len(ppg_data)))*100:.1f}",
                    f"{max(time_data):.2f}" if time_data else "0",
                    "50",
                    time.strftime('%Y-%m-%d %H:%M:%S')
                ]
            }
            df_summary = pd.DataFrame(summary_data)
            
            # Save to Excel dengan multiple sheets
            with pd.ExcelWriter(file_path, engine='openpyxl') as writer:
                df_aggregated.to_excel(writer, sheet_name='Data Agregat', index=False)
                df_raw.to_excel(writer, sheet_name='Raw Downsampled', index=False)
                df_summary.to_excel(writer, sheet_name='Summary', index=False)
            
            messagebox.showinfo("Berhasil", 
                              f"✅ Data disimpan ke {file_path}\n\n"
                              f"📊 BUFFERED VERSION:\n"
                              f"Sheet 1: Data Agregat ({len(aggregated_data)} records)\n"
                              f"Sheet 2: Raw Downsampled ({len(raw_downsampled)} samples)\n"
                              f"Sheet 3: Summary\n\n"
                              f"Efisiensi: {(1-len(aggregated_data)/max(1,len(ppg_data)))*100:.1f}% pengurangan!\n"
                              f"Dari {len(ppg_data)} → {len(aggregated_data)} records")
            
    except Exception as e:
        messagebox.showerror("Error", f"Gagal menyimpan:\n{str(e)}")

def save_png():
    """Save plot as PNG"""
    try:
        if fig is None:
            messagebox.showerror("Error", "Grafik tidak tersedia")
            return
        
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        subject_name = selected_subject.replace(" ", "_")
        default_filename = f"Grafik_HR_{subject_name}_{timestamp}.png"
        
        file_path = filedialog.asksaveasfilename(
            defaultextension=".png", 
            filetypes=[("File PNG", "*.png")],
            title="Simpan Grafik",
            initialfile=default_filename
        )
        
        if file_path:
            update_plot()
            fig.savefig(file_path, dpi=300, bbox_inches='tight', facecolor='white')
            messagebox.showinfo("Berhasil", f"Grafik disimpan ke {file_path}")
            
    except Exception as e:
        messagebox.showerror("Error", f"Gagal menyimpan:\n{str(e)}")

def close_app():
    """Close application"""
    global serial_running, db_conn
    
    try:
        serial_running = False
        if ser and ser.is_open:
            ser.close()
        
        if db_conn and not db_conn.closed:
            db_conn.close()
    except:
        pass
    
    if root is not None:
        root.destroy()

def refresh_plot_manually():
    """Manually refresh plot"""
    update_plot()

def setup_gui():
    """Initialize GUI"""
    global root, fig, ax1, ax2, canvas, status_label, data_count_label, latest_data_label
    global data_tree, analysis_text, port_label, db_status_label
    
    root = tk.Tk()
    root.title(f"Monitor Detak Jantung MAX30102 + PostgreSQL (BUFFERED) - {selected_subject}")
    root.protocol("WM_DELETE_WINDOW", close_app)
    root.state('zoomed')
    
    root.grid_rowconfigure(0, weight=0)
    root.grid_rowconfigure(1, weight=1)
    root.grid_rowconfigure(2, weight=0)
    root.grid_columnconfigure(0, weight=1)
    
    # Title
    title_frame = tk.Frame(root, bg="darkred", relief=tk.RAISED, bd=2)
    title_frame.grid(row=0, column=0, sticky="ew", padx=5, pady=5)
    
    title_label = tk.Label(title_frame, 
                          text="Sistem Monitoring Detak Jantung MAX30102 + PostgreSQL (BUFFERED - Anti Spam!)",
                          font=("Arial", 16, "bold"),
                          fg="white", bg="darkred", pady=10)
    title_label.pack()
    
    # Main content
    main_frame = tk.Frame(root, relief=tk.RAISED, bd=1)
    main_frame.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
    
    main_frame.grid_rowconfigure(0, weight=1)
    main_frame.grid_columnconfigure(0, weight=2)
    main_frame.grid_columnconfigure(1, weight=1)
    main_frame.grid_columnconfigure(2, weight=1)
    
    # Plot frame
    plot_frame = tk.LabelFrame(main_frame, text=f"Grafik Real-time - {selected_subject}", font=("Arial", 12, "bold"))
    plot_frame.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
    
    fig = Figure(figsize=(10, 8), dpi=100)
    ax1 = fig.add_subplot(211)
    ax2 = fig.add_subplot(212)
    
    ax1.set_xlabel("Waktu (detik)")
    ax1.set_ylabel("Amplitudo AC")
    ax1.set_title(f"Sinyal AC (Filtered) - {selected_subject}")
    ax1.grid(True, alpha=0.3)
    
    ax2.set_xlabel("Waktu (detik)")
    ax2.set_ylabel("Detak Jantung (BPM)")
    ax2.set_title("Detak Jantung Real-time")
    ax2.grid(True, alpha=0.3)
    
    canvas = FigureCanvasTkAgg(fig, master=plot_frame)
    canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
    
    # Data table
    table_frame = tk.LabelFrame(main_frame, text="Tabel Data (50 Terakhir)", font=("Arial", 12, "bold"))
    table_frame.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)
    
    table_container = tk.Frame(table_frame)
    table_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
    
    v_scrollbar = tk.Scrollbar(table_container, orient="vertical")
    h_scrollbar = tk.Scrollbar(table_container, orient="horizontal")
    
    columns = ('Indeks', 'Waktu (s)', 'AC', 'Threshold', 'Beat')
    data_tree = ttk.Treeview(table_container, columns=columns, show='headings',
                            yscrollcommand=v_scrollbar.set, xscrollcommand=h_scrollbar.set)
    
    data_tree.heading('Indeks', text='#')
    data_tree.heading('Waktu (s)', text='Waktu (s)')
    data_tree.heading('AC', text='AC')
    data_tree.heading('Threshold', text='Threshold')
    data_tree.heading('Beat', text='Beat')
    
    data_tree.column('Indeks', width=50)
    data_tree.column('Waktu (s)', width=80)
    data_tree.column('AC', width=80)
    data_tree.column('Threshold', width=80)
    data_tree.column('Beat', width=60)
    
    v_scrollbar.pack(side="right", fill="y")
    h_scrollbar.pack(side="bottom", fill="x")
    data_tree.pack(side="left", fill="both", expand=True)
    
    v_scrollbar.config(command=data_tree.yview)
    h_scrollbar.config(command=data_tree.xview)
    
    # Analysis frame
    analysis_frame = tk.LabelFrame(main_frame, text=f"Analisis Real-time - {selected_subject}", font=("Arial", 12, "bold"))
    analysis_frame.grid(row=0, column=2, sticky="nsew", padx=5, pady=5)
    
    analysis_container = tk.Frame(analysis_frame)
    analysis_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
    
    analysis_scrollbar = tk.Scrollbar(analysis_container)
    analysis_scrollbar.pack(side="right", fill="y")
    
    analysis_text = tk.Text(analysis_container, yscrollcommand=analysis_scrollbar.set,
                           font=("Courier", 9), wrap=tk.WORD, state=tk.DISABLED)
    analysis_text.pack(side="left", fill="both", expand=True)
    analysis_scrollbar.config(command=analysis_text.yview)
    
    # Control frame
    control_frame = tk.Frame(root, relief=tk.RAISED, bd=1)
    control_frame.grid(row=2, column=0, sticky="ew", padx=5, pady=5)
    
    # Status
    status_frame = tk.Frame(control_frame)
    status_frame.pack(fill=tk.X, padx=5, pady=2)
    
    status_label = tk.Label(status_frame, text="Status: Tidak Terhubung", font=("Arial", 14, "bold"), fg="red")
    status_label.pack(side=tk.LEFT)
    
    port_label = tk.Label(status_frame, text=f"Port: {DEFAULT_PORT} (default)", font=("Arial", 12))
    port_label.pack(side=tk.LEFT, padx=20)
    
    db_status_label = tk.Label(status_frame, text="Database: Tidak Terhubung", font=("Arial", 12), fg="red")
    db_status_label.pack(side=tk.LEFT, padx=20)
    
    latest_data_label = tk.Label(status_frame, text="Terbaru: -", font=("Arial", 12))
    latest_data_label.pack(side=tk.LEFT, padx=20)
    
    data_count_label = tk.Label(status_frame, text="Data: 0 | Buffer: 0 | Agregat: 0", font=("Arial", 12))
    data_count_label.pack(side=tk.RIGHT)
    
    # Serial connection controls
    serial_frame = tk.LabelFrame(control_frame, text="Koneksi Serial", font=("Arial", 11, "bold"))
    serial_frame.pack(fill=tk.X, padx=5, pady=3)
    
    tk.Button(serial_frame, text=f"Auto-Connect ({DEFAULT_PORT})", command=connect_serial_auto, 
             bg="lightgreen", width=20, font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=3, pady=3)
    tk.Button(serial_frame, text="Pilih Port Manual", command=connect_serial, 
             bg="lightblue", width=18, font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=3, pady=3)
    tk.Button(serial_frame, text="Putuskan Serial", command=disconnect_serial, 
             bg="lightcoral", width=15, font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=3, pady=3)
    
    # Database controls
    db_frame = tk.LabelFrame(control_frame, text="Koneksi Database", font=("Arial", 11, "bold"))
    db_frame.pack(fill=tk.X, padx=5, pady=3)
    
    tk.Button(db_frame, text="Hubungkan Database", command=connect_database, 
             bg="lightgreen", width=20, font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=3, pady=3)
    tk.Button(db_frame, text="Konfigurasi DB", command=configure_database, 
             bg="lightblue", width=18, font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=3, pady=3)
    tk.Button(db_frame, text="Lihat Data Tersimpan", command=view_database_records, 
             bg="lightyellow", width=18, font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=3, pady=3)
    tk.Button(db_frame, text="Putuskan Database", command=disconnect_database, 
             bg="lightcoral", width=18, font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=3, pady=3)
    
    # Config
    config_frame = tk.LabelFrame(control_frame, text="Konfigurasi", font=("Arial", 11, "bold"))
    config_frame.pack(fill=tk.X, padx=5, pady=3)

    global patient_button

    patient_button = tk.Button(config_frame, text=f"Pasien: {selected_subject}", 
            command=set_subject, bg="lightblue", width=25, font=("Arial", 10, "bold"))
    patient_button.pack(side=tk.LEFT, padx=3, pady=3)

    tk.Label(config_frame, text=f"📊 Buffer: {BUFFER_SIZE} | Downsample: 1/{DOWNSAMPLE_RATE}", 
            font=("Arial", 10), fg="darkgreen").pack(side=tk.LEFT, padx=20)
    
    # Main controls
    main_control_frame = tk.LabelFrame(control_frame, text="Kontrol Utama", font=("Arial", 11, "bold"))
    main_control_frame.pack(fill=tk.X, padx=5, pady=3)
    
    tk.Button(main_control_frame, text="Mulai Pengukuran", command=start_collection, 
             bg="lightgreen", width=15, font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=3, pady=3)
    tk.Button(main_control_frame, text="Hentikan Pengukuran", command=stop_collection, 
             bg="lightcoral", width=18, font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=3, pady=3)
    tk.Button(main_control_frame, text="Reset Data", command=reset_data, 
             bg="lightyellow", width=12, font=("Arial", 10)).pack(side=tk.LEFT, padx=3, pady=3)
    tk.Button(main_control_frame, text="Refresh Grafik", command=refresh_plot_manually, 
             bg="lightcyan", width=12, font=("Arial", 10)).pack(side=tk.LEFT, padx=3, pady=3)
    
    # Analysis controls
    analysis_control_frame = tk.LabelFrame(control_frame, text="Analisis & Ekspor (BUFFERED)", font=("Arial", 11, "bold"))
    analysis_control_frame.pack(fill=tk.X, padx=5, pady=3)
    
    tk.Button(analysis_control_frame, text="Analisis Detak Jantung", command=calculate_heart_rate_statistics, 
             bg="orange", width=20, font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=3, pady=3)
    tk.Button(analysis_control_frame, text="💾 Simpan ke Database", command=save_to_database, 
             bg="#90EE90", width=20, font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=3, pady=3)
    tk.Button(analysis_control_frame, text="Simpan Hasil Analisis", command=save_analysis_to_file, 
             bg="lightgreen", width=18, font=("Arial", 10)).pack(side=tk.LEFT, padx=3, pady=3)
    tk.Button(analysis_control_frame, text="📊 Simpan Excel (Agregat)", command=save_excel, 
             bg="lightblue", width=18, font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=3, pady=3)
    tk.Button(analysis_control_frame, text="Simpan Grafik (PNG)", command=save_png, 
             bg="lightblue", width=15, font=("Arial", 10)).pack(side=tk.LEFT, padx=3, pady=3)
    tk.Button(analysis_control_frame, text="Keluar", command=close_app, 
             bg="lightgray", width=10, font=("Arial", 10, "bold")).pack(side=tk.RIGHT, padx=3, pady=3)
    
    update_analysis_display()

def main():
    """Main entry point"""
    print("="*60)
    print("  MONITOR DETAK JANTUNG MAX30102 + POSTGRESQL")
    print("  ✨ BUFFERED VERSION - ANTI SPAM! ✨")
    print("="*60)
    print(f"Serial Port: {DEFAULT_PORT} @ {DEFAULT_BAUDRATE}")
    print(f"Database: {DB_CONFIG['database']} @ {DB_CONFIG['host']}:{DB_CONFIG['port']}")
    print(f"Buffer Size: {BUFFER_SIZE} samples (~{BUFFER_SIZE/50:.1f} detik @ 50Hz)")
    print(f"Downsample Rate: 1/{DOWNSAMPLE_RATE}")
    print("="*60)
    
    setup_gui()
    
    # Auto-connect on startup
    print("\n🔌 Mencoba auto-connect ke", DEFAULT_PORT, "...")
    root.after(500, connect_serial_auto)
    
    root.after(100, periodic_update)
    
    print("✅ Aplikasi dimulai!")
    print("="*60)
    
    root.mainloop()

if __name__ == "__main__":
    main()